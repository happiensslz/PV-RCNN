import torch
import math
from torch import nn
import torch.nn.functional as F

from pvrcnn.ops import sigmoid_focal_loss, batched_nms_rotated


class ProposalLayer(nn.Module):
    """Use BEV feature map to generate 3D box proposals."""

    def __init__(self, cfg):
        super(ProposalLayer, self).__init__()
        self.cfg = cfg
        self.conv_cls = nn.Conv2d(
            cfg.PROPOSAL.C_IN, cfg.NUM_CLASSES * cfg.NUM_YAW, 1)
        self.conv_reg = nn.Conv2d(
            cfg.PROPOSAL.C_IN, cfg.NUM_CLASSES * cfg.NUM_YAW * cfg.BOX_DOF, 1)
        self._init_weights()

    def _init_weights(self):
        nn.init.constant_(self.conv_cls.bias, (-math.log(1 - .01) / .01))
        nn.init.constant_(self.conv_reg.bias, 0)
        for m in (self.conv_cls.weight, self.conv_reg.weight):
            nn.init.normal_(m, std=0.01)

    def inference(self, feature_map):
        """TODO: Sigmoid and topk proposal indexing."""
        cls_map, reg_map = self(feature_map)
        scores = cls_map.sigmoid()
        raise NotImplementedError

    def reshape_cls(self, cls_map):
        B, _, ny, nx = cls_map.shape
        shape = (B, self.cfg.NUM_CLASSES, self.cfg.NUM_YAW, ny, nx)
        cls_map = cls_map.view(shape)
        return cls_map

    def reshape_reg(self, reg_map):
        B, _, ny, nx = reg_map.shape
        shape = (B, self.cfg.NUM_CLASSES, self.cfg.BOX_DOF, -1, ny, nx)
        reg_map = reg_map.view(shape).permute(0, 1, 3, 4, 5, 2)
        return reg_map

    def forward(self, feature_map):
        cls_map = self.reshape_cls(self.conv_cls(feature_map))
        reg_map = self.reshape_reg(self.conv_reg(feature_map))
        return cls_map, reg_map


class ProposalLoss(nn.Module):
    """
    Notation: (P_i, G_i, M_i) ~ (predicted, ground truth, mask).
    Loss is averaged by number of positive examples.
    TODO: Replace with compiled cuda focal loss.
    """

    def __init__(self, cfg):
        super(ProposalLoss, self).__init__()
        self.cfg = cfg

    def masked_average(self, loss, mask):
        mask = mask.type_as(loss)
        loss = (loss * mask).sum()
        return loss

    def reg_loss(self, P_reg, G_reg, M_reg):
        """Loss applied at all positive sites."""
        P_xyz, P_wlh, P_yaw = P_reg.split([3, 3, 1], dim=-1)
        G_xyz, G_wlh, G_yaw = G_reg.split([3, 3, 1], dim=-1)
        loss_xyz = F.smooth_l1_loss(P_xyz, G_xyz, reduction='none')
        loss_wlh = F.smooth_l1_loss(P_wlh, G_wlh, reduction='none')
        loss_yaw = F.smooth_l1_loss(P_yaw, G_yaw, reduction='none') / math.pi
        loss = self.masked_average(loss_xyz + loss_wlh + loss_yaw, M_reg)
        return loss

    def cls_loss(self, P_cls, G_cls, M_cls):
        """Loss is applied at all non-ignore sites. Assumes logit scores."""
        loss = sigmoid_focal_loss(P_cls, G_cls.float(), reduction='none')
        loss = self.masked_average(loss, M_cls)
        return loss

    def forward(self, item):
        keys = ['G_cls', 'M_cls', 'P_cls', 'G_reg', 'M_reg', 'P_reg']
        G_cls, M_cls, P_cls, G_reg, M_reg, P_reg = map(item.get, keys)
        normalizer = M_reg.type_as(P_reg).sum().clamp_(min=1)
        cls_loss = self.cls_loss(P_cls, G_cls, M_cls) / normalizer
        reg_loss = self.reg_loss(P_reg, G_reg, M_reg) / normalizer
        loss = cls_loss + self.cfg.TRAIN.LAMBDA * reg_loss
        losses = dict(cls_loss=cls_loss, reg_loss=reg_loss, loss=loss)
        return losses
