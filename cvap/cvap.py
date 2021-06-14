import os
import warnings
from typing import Union, List

import time
import torch
from torch import nn
from PIL import Image
from tqdm import tqdm

from .text_head import build_text_head
from .loss_head import build_loss_head
from .image_head import build_image_head
from .audio_head import build_audio_head

from .datasets import PairImageSpectrogramTFRecords

from clip import load 

class Monitor(nn.Module):

    def __init__(self, cfg, echo):
        super(Monitor, self).__init__()
        self.cfg = cfg
        self.echo = echo
        self.build_data()
        tunable_params = self.build_model()
        self.build_optimizer(tunable_params)

    def build_data(self):
        rcfg = self.cfg.running
        data_path = f"{rcfg.data_root}/{rcfg.data_name}"
        self.dataloader = PairImageSpectrogramTFRecords(
            data_path, 
            rcfg.batch_size,
            max_audio_len=rcfg.max_audio_len
        )

    def learn(self):
        if not self.training:
            with torch.no_grad():
                return self.infer()
        self.echo("Training started...")
        self.total_loss = 0
        self.total_step = 0
        self.total_inst = 0
        self.start_time = time.time()
        #self.save() 
        for iepoch in range(self.cfg.running.epochs):
            self.epoch(iepoch)
            if iepoch > 2:
                break

    def make_batch(self, batch):
        images, audios = batch["image"], batch["audio"]
        images = torch.tensor(images).cuda()
        audios = torch.tensor(audios).unsqueeze(1).cuda()
        return images, audios

    def epoch(self, iepoch):
        for batch in self.dataloader:
            images, audios = self.make_batch(batch)

            image_features = self.image_head(images)
            audio_features = self.audio_head(audios)
            loss = self.loss_head(image_features, audio_features)    

            self.optimizer.zero_grad()
            loss.backward()
            if self.cfg.optimizer.max_norm > 0:
                torch.nn.utils.clip_grad.clip_grad_norm_(
                    self.params, self.cfg.optimizer.max_norm
                )
            self.optimizer.step()

            self.total_step += 1
            self.total_loss += loss.item()
            self.total_inst += images.shape[0] 
            if self.total_step % self.cfg.running.peep_rate == 0:
                self.echo(
                    f"epoch {iepoch}\tstep {self.total_step}\t" +
                    f"loss {self.total_loss / self.total_step:.5f}\t" + 
                    f"{self.total_inst / (time.time() - self.start_time):.2f} samples/s"
                )
            if self.total_step % self.cfg.running.save_rate == 0:
                self.save()
        
    def infer(self):
        pass
    
    def togpu(self):
        self.image_head.cuda()
        self.audio_head.cuda()
        self.loss_head.cuda()

    def save(self):
        fsave = f"{self.cfg.model_root}/{self.cfg.model_name}/{self.total_step:08d}.pth"
        self.echo(f"Saving the checkpoint to {fsave}")
        checkpoint = {
            "cfg": self.cfg,
            "model": (
                self.image_head.cpu().state_dict(), 
                self.audio_head.cpu().state_dict(),
                self.loss_head.cpu().state_dict(),
            )
        }
        torch.save(checkpoint, fsave)
        self.togpu()

    def build_model(self):
        tunable_params = dict()
        if self.cfg.eval:
            self.echo(f"Loading from {self.cfg.model_file}")
            checkpoint = torch.load(self.cfg.model_file, map_location="cpu")
            local_cfg = checkpoint["cfg"]
            self.echo(f"Old configs:\n{local_cfg}")
            image_head_sd, audio_head_sd = checkpoint["model"]
            self.image_head = build_image_head(local_cfg.model.image)
            self.image_head.load_state_dict(image_head_sd)
            self.audio_head = build_audio_head(local_cfg.model.audio)
            self.audio_head.load_state_dict(audio_head_sd)
            self.eval()
        else:
            rcfg = self.cfg.running
            model, self.T = load(
                rcfg.clip_model_name, rcfg.clip_model_root, device="cpu", jit=False
            )
            image_head_sd = model.visual.state_dict()
            self.image_head = build_image_head(self.cfg.model.image)
            self.image_head.copy_state_dict(image_head_sd)

            self.audio_head = build_audio_head(self.cfg.model.audio)
            self.audio_head.copy_state_dict(image_head_sd)
                
            extra_sd = {"logit_scale": model.logit_scale}
            self.loss_head = build_loss_head(self.cfg.model.loss)
            self.loss_head.copy_state_dict(extra_sd)

            audio_head_sd = self.audio_head.state_dict()
            tunable_params = {f"audio_head.{k}": v for k, v in audio_head_sd.items()} 
            tunable_params.update(
                {f"loss_head.{k}": v for k, v in self.loss_head.state_dict().items()}
            )
        self.togpu()
        return tunable_params

    def build_optimizer(self, tunable_params={}):
        self.params = (
            list(tunable_params.values())
        )
        for k, v in tunable_params.items():
            pass #self.echo(f"{k} {v.size()}")
        for k, v in self.image_head.named_parameters():
            if f"image_head.{k}" not in tunable_params:
                v.requires_grad = False
        for k, v in self.audio_head.named_parameters():
            if f"audio_head.{k}" not in tunable_params:
                v.requires_grad = False
        for k, v in self.loss_head.named_parameters():
            if f"loss_head.{k}" not in tunable_params:
                v.requires_grad = False
        self.optimizer = getattr(torch.optim, self.cfg.optimizer.name)(
            self.params, 
            lr=self.cfg.optimizer.lr, 
            weight_decay=self.cfg.optimizer.weight_decay
        )
        debug = False 
        if not debug: 
            return
        self.echo(f"Gradienting The Following Parameters:")
        for k, v in self.image_head.named_parameters():
            if v.requires_grad:
                self.echo(f"image_head.{k} {v.size()}")
        for k, v in self.audio_head.named_parameters():
            if v.requires_grad:
                self.echo(f"audio_head.{k} {v.size()}")
        for k, v in self.loss_head.named_parameters():
            if v.requires_grad:
                self.echo(f"loss_head.{k} {v.size()}")

    def forward(self, *args, **kwargs):
        pass
