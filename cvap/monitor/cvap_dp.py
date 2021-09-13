from omegaconf import OmegaConf
import os, re
from collections import defaultdict

import time
import torch
import numpy as np
from torch import nn

import tensorflow as tf
import torch.distributed as dist
import torch.nn.functional as F
from torch.nn.parallel import data_parallel
from torch.nn.parallel import DistributedDataParallel
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts

from ..util import numel
from ..model import build_main_model
from ..module import LARS, exclude_bias_or_norm, adjust_learning_rate
from ..dataset import build_dataloader 

class Monitor(object):
    def __init__(self, cfg, echo, device):
        super(Monitor, self).__init__()
        self.cfg = cfg
        self.echo = echo
        self.device = device
        self.build_data()
        if self.cfg.running.audio.eval_norms:
            self.eval_norms()
            return # mean & std of the data
        model = build_main_model(cfg, echo)
        tunable_params = model.build()
        self.model = DistributedDataParallel(
            model, device_ids=[cfg.rank], find_unused_parameters=True
        ) if torch.distributed.is_initialized() else model 
        self.model.train(not cfg.eval)
        self.build_optimizer(tunable_params)

    def eval_norms(self):
        self.echo("Evaluate mean and std...")
        cnt = 0.
        som = torch.tensor(0., device=self.device)
        sos = torch.tensor(0., device=self.device)
        som_list, sos_list = list(), list()
        for step, batch in enumerate(self.dataloader):
            _, audios, _ = self.make_batch(batch)
            bsz = audios.shape[0]
            new_cnt = cnt + bsz
            mean = audios.mean(axis=[2, 3])
            mean_sq = (audios ** 2).mean(axis=[2, 3])
            """ incremental update might be numerically unstable
            som = (som * cnt + mean.sum()) / new_cnt
            sos = (sos * cnt + mean_sq.sum()) / new_cnt
            """
            som_list.append(mean)
            sos_list.append(mean_sq)
            self.echo(f"step {step}")
        som = torch.cat(som_list, 0).mean(axis=[0])
        sos = torch.cat(sos_list, 0).mean(axis=[0])
        std = (sos - som ** 2).sqrt()
        self.echo(f"MEAN: {som.cpu().tolist()} STD: {std.cpu().tolist()}")

    def build_data(self):
        rcfg = self.cfg.running
        data_name = rcfg.eval_name if self.cfg.eval else rcfg.data_name
        _, self.dataloader = build_dataloader(
            self.cfg, data_name, shuffle=(not self.cfg.eval), train=(not self.cfg.eval)
        )
        nstep = len(self.dataloader) 
        if nstep < self.cfg.running.peep_rate:
            self.cfg.running.peep_rate = nstep 
        self.echo(f"Instantiate main dataloader from `{data_name}': total {nstep} ({self.cfg.running.peep_rate}) batches.")
        self.gold_file = f"{rcfg.data_root}/{data_name}.csv"
        # evaluation
        eval_name = "IGNORE_ME" if self.cfg.eval else rcfg.eval_name
        data_path = f"{rcfg.data_root}/{eval_name}"
        do_eval = os.path.isdir(data_path) or os.path.isfile(f"{data_path}.csv") or tf.io.gfile.exists(f"{data_path}.csv")
        _, self.evalloader = build_dataloader(
            self.cfg, eval_name, shuffle=False, train=False
        ) if do_eval else (None, None)
        if self.evalloader is not None:
            self.echo(f"Will do evaluation every {rcfg.save_rate} steps on {len(self.evalloader)} batches.")
            self.gold_file = f"{rcfg.data_root}/{eval_name}.csv"

    def learn(self):
        if self.cfg.running.audio.eval_norms:
            return # `eval_norms` is the only task
        if not self.model.training:
            self.echo("Evaluating started...")
            with torch.no_grad():
                report = self.infer(self.dataloader, samples=self.cfg.running.eval_samples)
                self.echo(f"{report}")
                return None 
        self.echo("Training started...")
        self.last_time = 0.
        self.total_loss = 0
        self.total_step = 0
        self.total_inst = 0
        self.start_time = time.time()
        self.scaler = torch.cuda.amp.GradScaler()
        #self.save() 
        for iepoch in range(self.cfg.optimizer.epochs):
            if isinstance(self.model, DistributedDataParallel):
                self.dataloader.sampler.set_epoch(iepoch)
            if iepoch >= 1:
                pass #break
            self.epoch(iepoch)

    def make_batch(self, batch):
        images = torch.tensor(batch[0], device=self.device) # (c, h, w)
        audios = torch.tensor(batch[1], device=self.device).unsqueeze(1)
        if images.dim() != 2 and images.shape[-1] != self.cfg.running.resolution:
            images = F.interpolate(
                images,
                self.cfg.running.resolution,
                mode="bilinear",
                align_corners=False,
            )
        batch = (
            images, audios, batch[2], # sample id or name
        )
        return batch # bare tensors

        images, audios = batch["image"], batch["audio"]

        images = images.cuda(self.cfg.rank, non_blocking=True)
        audios = audios.cuda(self.cfg.rank, non_blocking=True)
        return images, audios # directly return dict of tensors

        images = torch.tensor(
            np.concatenate(images, axis=0), device=self.device
        ) #.cuda(self.cfg.rank, non_blocking=True)
        audios = torch.tensor(
            np.concatenate(audios, axis=0), device=self.device
        ).unsqueeze(1) #.cuda(self.cfg.rank, non_blocking=True)
        #print(f"make_batch {dist.get_rank()} {batch['name']}")
        return images, audios

    def timeit(self, time_dict, key=None, show=False):
        if self.cfg.rank != 0:
            return 
        if show: # print
            report = ""
            for k, v in time_dict.items():
                report += f"{k} {np.mean(v):.2f} "
            self.echo(f"Time (s): {report.strip()}; # step {self.total_step} # sample {self.total_inst}")
            return
        if key is None: # initialize
            self.last_time = time.time()
        else: # update
            this_time = time.time()
            time_dict[key].append(this_time - self.last_time)
            self.last_time = this_time

    def epoch(self, iepoch):
        all_time = defaultdict(list)
        self.timeit(all_time)        
        device_ids = [i for i in range(self.cfg.num_gpus)]
        nchunk = dist.get_world_size() if torch.distributed.is_initialized() else 1  
        warmup_step_rate = max(self.cfg.optimizer.warmup_steps // 20, 1)
        for step, batch in enumerate(self.dataloader, start=iepoch * len(self.dataloader)):
            images, audios, _ = self.make_batch(batch)
            self.timeit(all_time, key="data")

            if self.cfg.optimizer.use_lars:
                adjust_learning_rate(self.cfg.optimizer, self.optimizer, self.dataloader, step)

            inc = 0
            force_eval = False # recommended by SGDR
            warmup = not self.cfg.optimizer.use_lars and self.cfg.optimizer.warmup and \
                (self.total_step + inc) <= self.cfg.optimizer.warmup_steps
            if warmup and (self.total_step + inc) % warmup_step_rate == 0:
                ratio = ((self.total_step + inc) / self.cfg.optimizer.warmup_steps) # * self.cfg.optimizer.lr
                for param_group in self.optimizer.param_groups:
                    param_group['lr'] = ratio * param_group["initial_lr"]
                lrs = [param_group['lr'] for param_group in self.optimizer.param_groups]
                force_eval = lrs == self.scheduler.base_lrs
                lrs = [f"{lr:.2e}" for lr in lrs]
                self.echo(f"warmup lr: {' '.join(lrs)}")

            self.optimizer.zero_grad(set_to_none=True)
            with torch.cuda.amp.autocast():
                loss = self.model(images, audios, None, device_ids=device_ids)
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()

            if not self.cfg.optimizer.use_lars and self.cfg.optimizer.batch_sch and not warmup:
                old_lrs = " ".join([f"{x:.2e}" for x in self.scheduler.get_last_lr()])
                self.scheduler.step() # after all warmup is completed
                if isinstance(self.scheduler, (CosineAnnealingWarmRestarts,)):
                    force_eval = self.scheduler.get_last_lr() == self.scheduler.base_lrs
                #self.echo(f"do step lr {old_lrs}")

            self.timeit(all_time, key="model")

            if False and self.cfg.rank == 0:
                print(f"doing some check on unused params... {dist.get_world_size()}")
                for k, v in self.model.named_parameters():
                    if v.requires_grad and v.grad is None:
                        print(f"--> {k}")

            self.total_step += 1
            self.total_loss += loss.detach()
            self.total_inst += images.shape[0] * nchunk
            if force_eval or (self.cfg.rank == 0 and self.total_step % self.cfg.running.peep_rate == 0):
                def grad_norm():
                    return sum(
                        [p.grad.norm(p=2) ** 2 for p in self.params if p.grad is not None]
                    ).item() ** 0.5
                lr_w = self.optimizer.param_groups[0]['lr']
                lr_b = self.optimizer.param_groups[1]['lr']
                self.echo(
                    f"epoch {iepoch:>4} step {self.total_step}\t" + #gnorm {grad_norm():.2f} " +
                    f"lr_w {lr_w:.2e} lr_b {lr_b:.2e} loss {self.total_loss / self.total_step:.3f} " + 
                    f"{self.total_inst / (time.time() - self.start_time):.2f} samples/s" 
                )
            if force_eval or self.total_step % self.cfg.running.save_rate == 0 or (
                    self.cfg.running.save_epoch and self.total_step % len(self.dataloader) == 0
                ): # distributed eval
                report = ""
                if self.evalloader is not None:
                    self.model.train(False)
                    with torch.no_grad():
                        report = self.infer(
                            self.evalloader, samples=self.cfg.running.eval_samples, iepoch=iepoch
                        )
                    self.model.train(True)
                if report != "":
                    self.echo(f"{report}")
                if self.cfg.rank == 0:
                    self.save()
            self.timeit(all_time, key="report")

        if not self.cfg.optimizer.use_lars and not self.cfg.optimizer.batch_sch:
            self.scheduler.step()
        self.timeit(all_time, show=True)
        
    def infer(self, dataloader, samples=float("inf"), iepoch=0):
        losses, nsample, nchunk, nbatch = 0, 0, 1, len(dataloader)
        device_ids = [i for i in range(self.cfg.num_gpus)]
        if isinstance(self.model, DistributedDataParallel):
            dataloader.sampler.set_epoch(iepoch)
            nchunk = self.cfg.num_gpus
        peep_rate = max(10, (len(dataloader) // 10))
        start_time = time.time()
        for ibatch, batch in enumerate(dataloader):
            if nsample >= samples:
                #print(f"{nsample}\t{ibatch}/{nbatch} continue")
                break #continue # iterate through every batch 
            images, audios, names = self.make_batch(batch)
            #msg = f"{images[0, 0, 50, 50:55]} {audios[0, 0, 50, 50:55]}" # if ibatch == 0 else ""
            #print(f"{nsample}\t{ibatch}/{nbatch} done {msg}")
            loss = self.model(images, audios, None, device_ids=device_ids, names=names)
            nsample += images.shape[0] * nchunk
            losses += loss or 0.
            if self.cfg.rank == 0 and (ibatch + 1) % peep_rate == 0:
                self.echo(
                    f"step {ibatch}\t" + #gnorm {grad_norm():.2f} " +
                    f"loss {losses / (ibatch + 1):.8f} " +
                    f"{nsample / (time.time() - start_time):.2f} samples/s"
                )
        model = self.model.module if isinstance(self.model, DistributedDataParallel) else self.model
        self.echo(f"# sample {nsample}; {nsample / (time.time() - start_time):.2f} samples/s")
        return model.report(gold_file=self.gold_file)

    def save(self):
        fsave = f"{self.cfg.alias_root}/{self.cfg.model_name}/{self.total_step:08d}.pth"
        self.echo(f"Saving the checkpoint to {fsave}")
        model = self.model.module if isinstance(self.model, DistributedDataParallel) else self.model
        checkpoint = {
            "cfg": self.cfg, "model": model.collect_audio_state_dict(), # model.collect_state_dict(),
        }
        torch.save(checkpoint, fsave)

    def build_optimizer(self, tunable_params={}):
        if not self.model.training:
            return
        self.params = (
            list(tunable_params.values())
        )
        for k, v in tunable_params.items():
            if self.cfg.rank == 0:
                pass #self.echo(f"{k} {v.size()}")
        ddp = isinstance(self.model, DistributedDataParallel)
        for k, v in self.model.named_parameters():
            k = re.sub("^module\.", "", k) if ddp else k
            if f"{k}" not in tunable_params:
                v.requires_grad = False
        self.echo(f"# param {numel(self.model) / 1e6:.2f}M # tunable {numel(self.model, True) / 1e6:.2f}M.")
        param_groups = [
            {"params": [p for p in self.params if p.ndim > 1]},
            {"params": [p for p in self.params if p.ndim < 2]},
        ]
        if self.cfg.optimizer.use_lars:
            self.optimizer = LARS(
                param_groups,
                lr=0.,
                weight_decay=self.cfg.optimizer.weight_decay,
                weight_decay_filter=exclude_bias_or_norm,
                lars_adaptation_filter=exclude_bias_or_norm,
            )
        else:
            ocfg = self.cfg.optimizer.optimizer
            scfg = self.cfg.optimizer.scheduler
            self.optimizer = getattr(torch.optim, ocfg[0])(param_groups, **ocfg[1])
            self.scheduler = getattr(torch.optim.lr_scheduler, scfg[0])(self.optimizer, **scfg[1])
        if not self.cfg.verbose:
            return
        self.echo(f"Gradienting The Following Parameters:")
        for k, v in self.model.named_parameters():
            if v.requires_grad:
                self.echo(f"{k} {v.size()}")

