from collections import OrderedDict
from typing import Tuple, Union
from fvcore.common.registry import Registry
from sklearn import metrics

import math
import copy
import json
import threading
import numpy as np
import torch
import torch.nn.functional as F
import torch.distributed as dist
from torch import nn

from collections import defaultdict
from clip import LayerNorm, Transformer, ModifiedResNet, VisualTransformer  

from .loss_head import build_loss_head, LossHead


class BCELossHead(LossHead):
    def __init__(self, cfg, **kwargs):
        super().__init__()
        self.normalized = False
        assert "output_dim" in kwargs, f"`the label number` is not found in `kwargs`"
        nlabel = kwargs["output_dim"]
        self.linear = nn.Sequential(
            LayerNorm(cfg.embed_dim), 
            nn.Linear(cfg.embed_dim, nlabel),
        )  
        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        self.loss_fn = nn.BCEWithLogitsLoss()
        self.reduce = False 
    
    def copy_state_dict(self, state_dict): 
        key = "logit_scale"
        new_dict = self.state_dict()
        new_dict.update({key: state_dict[key]})
        self.load_state_dict(new_dict)

    def infer(self, x1, x2, *args, **kwargs):
        if not hasattr(self, "audios") or not hasattr(self, "x1s") or \
            not hasattr(self, "x2s") or not hasattr(self, "ids"): 
            self.audios, self.x1s, self.x2s, self.ids = [], [], [], []
        self.audios.append(x1)
        logit_scale = self.logit_scale.exp()
        logits_per_x1 = logit_scale * self.linear(x1)
        predictions = torch.sigmoid(logits_per_x1)
        self.x1s.append(predictions)
        self.x2s.append(x2)
        names = kwargs.get("names", None)
        if names is not None:
            self.ids.extend(names)
        return None 

    def report(self, gold_file=None, **kwargs):
        x1s = torch.cat(self.x1s).cpu().numpy()
        x2s = torch.cat(self.x2s).cpu().numpy()
        nsample, nlabel = x1s.shape[:2]
        
        ap_micro = metrics.average_precision_score(x2s, x1s, average='micro')
        ap_macro = metrics.average_precision_score(x2s, x1s, average='macro')
        ap_weighted = metrics.average_precision_score(x2s, x1s, average='weighted')

        # multi-label classification metrics
        has_err = False
        ap_list, auc_list, precisions, recalls = [], [], [], []
        for k in range(nlabel): # unnecessary (from AST)
            y_true, y_score = x2s[:, k], x1s[:, k]
            ap = metrics.average_precision_score(y_true, y_score, average=None) 
            if math.isnan(ap):
                ap = 0.
                has_err = True
            try:
                auc = metrics.roc_auc_score(y_true, y_score, average=None)
            except Exception as e:
                auc = 0. # auc may not be used a valid metric for this task
                has_err = True
            p, r, _ = metrics.precision_recall_curve(y_true, y_score)
            mid = len(p) // 2
            ap_list.append(ap)
            auc_list.append(auc)
            precisions.append(p[mid])
            recalls.append(r[mid])
        mean_ap = np.mean(ap_list) * 100.
        mean_auc = np.mean(auc_list) * 100.
        mean_p = np.mean(precisions) * 100.
        mean_r = np.mean(recalls) * 100.
        text = (
            f"Err({has_err}) mAP = {mean_ap:2.2f} mAUC = {mean_auc:2.2f} mP = {mean_p:2.2f} mR = {mean_r:2.2f}"
        )

        del self.audios, self.x1s, self.x2s, self.ids
        common = f"Mac-AP = {ap_macro:2.2f} Mic-AP = {ap_micro:2.2f} wAP = {ap_weighted:2.2f}"
        report = f"{common} {text} @ {nsample}" 
        return report

    def forward(self, x1, x2, *args, **kwargs):
        """ x1 is the input features and x2 is the label
        """
        if not self.training:
            if not dist.is_initialized() or dist.get_rank() == 0:
                return self.infer(x1, x2, *args, **kwargs)
            return None 
        logit_scale = self.logit_scale.exp()
        logits_per_x1 = logit_scale * self.linear(x1)
        loss_mean_x1 = self.loss_fn(logits_per_x1, x2.float())
        return loss_mean_x1

class BCEAndCELossHead(LossHead):
    # combining binary cross-entropy loss and cross-entropy loss 
    def __init__(self, cfg, **kwargs):
        super().__init__()
        self.normalized = False
        self.loss_ce = build_loss_head(cfg.ce, **kwargs)
        self.loss_bce = build_loss_head(cfg.bce, **kwargs)
        self.lambd_ce = cfg.lambd_ce
        self.reduce = True 

    def report(self, gold_file=None):
        report_ce = ""
        if hasattr(self.loss_ce, "x1s") and hasattr(self.loss_ce, "x2s"):
            report_ce = self.loss_ce.report(gold_file=gold_file)
        report_bce = self.loss_bce.report(gold_file=gold_file) 
        return f"{report_ce}\n{report_bce}" 
    
    def forward(self, x1, x2, *args, x3=None, **kwargs):
        """ x1 is features, x2 is labels, and x3 is mirror features
        """
        if not self.training:
            if not dist.is_initialized() or dist.get_rank() == 0:
                if x3 is not None:
                    self.loss_ce.infer(x1, x3, *args, **kwargs)
                return self.loss_bce.infer(x1, x2, *args, **kwargs)
            return None
        loss_ce = self.loss_ce(x1, x3, *args, **kwargs)
        loss_bce = self.loss_bce(x1, x2, *args, **kwargs)
        loss = self.lambd_ce * loss_ce + loss_bce
        return loss
