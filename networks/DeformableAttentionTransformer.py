# --------------------------------------------------------
# Swin Transformer
# Copyright (c) 2021 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ze Liu
# --------------------------------------------------------
# Vision Transformer with Deformable Attention
# Modified by Zhuofan Xia
# --------------------------------------------------------

import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from timm.models.layers import DropPath, to_2tuple


#from .DeformableAttention import LayerNormProxy, TransformerMLPWithConv, DAttentionBaseline, LocalAttention
from .DeformableAttention import LayerNormProxy, TransformerMLPWithConv, DAttentionBaseline, LocalAttention, ShiftWindowAttention
#from .segformer import MixFFN_skip
from .segformer import MixFFN_skip

#from .slide import SlideAttention
#from .dat_blocks import *
#from .nat import NeighborhoodAttention2D
#from .qna import FusedKQnA


class LayerScale(nn.Module):

    def __init__(self,
                 dim: int,
                 inplace: bool = False,
                 init_values: float = 1e-5):
        super().__init__()
        self.inplace = inplace
        self.weight = nn.Parameter(torch.ones(dim) * init_values)

    def forward(self, x):
        if self.inplace:
            return x.mul_(self.weight.view(-1, 1, 1))
        else:
            return x * self.weight.view(-1, 1, 1)

class TransformerStage(nn.Module):

    def __init__(self, fmap_size, window_size, ns_per_pt,
                 dim_in, dim_embed, depths, stage_spec, n_groups,
                 use_pe,sr_ratio,
                 heads, heads_q,
                 stride,
                 offset_range_factor,
                 dwc_pe, no_off, fixed_pe,
                 attn_drop, proj_drop, expansion, drop, drop_path_rate,
                 use_dwc_mlp, ksize,
                 nat_ksize,
                 k_qna,
                 nq_qna,
                 qna_activation,
                 layer_scale_value, use_lpu, log_cpb):

        super().__init__()
        fmap_size = to_2tuple(fmap_size)
        self.depths = depths
        hc = dim_embed // heads
        assert dim_embed == heads * hc
        self.proj = nn.Conv2d(dim_in, dim_embed, 1, 1, 0) if dim_in != dim_embed else nn.Identity()
        self.stage_spec = stage_spec
        self.use_lpu = use_lpu

        self.ln_cnvnxt = nn.ModuleDict(
            {str(d): LayerNormProxy(dim_embed) for d in range(depths) if stage_spec[d] == 'X'}
        )
        self.layer_norms = nn.ModuleList(
            [LayerNormProxy(dim_embed) if stage_spec[d // 2] != 'X' else nn.Identity() for d in range(2 * depths)]
        )

        mlp_fn = TransformerMLPWithConv if use_dwc_mlp else MixFFN_skip

        self.mlps = nn.ModuleList(
            [
                mlp_fn(dim_embed, expansion, drop) if use_dwc_mlp else mlp_fn(dim_embed, dim_embed*expansion)for _ in range(depths)
            ]
        )
        self.attns = nn.ModuleList()
        self.drop_path = nn.ModuleList()
        self.layer_scales = nn.ModuleList(
            [
                LayerScale(dim_embed, init_values=layer_scale_value) if layer_scale_value > 0.0 else nn.Identity()
                for _ in range(2 * depths)
            ]
        )
        self.local_perception_units = nn.ModuleList(
            [
                nn.Conv2d(dim_embed, dim_embed, kernel_size=3, stride=1, padding=1, groups=dim_embed) if use_lpu else nn.Identity()
                for _ in range(depths)
            ]
        )

        for i in range(self.depths):

            if stage_spec[i] == 'D':
                self.attns.append(
                    DAttentionBaseline(fmap_size, fmap_size, heads,
                    hc, n_groups, attn_drop, proj_drop,
                    stride, offset_range_factor, use_pe, dwc_pe,
                    no_off, fixed_pe, ksize, log_cpb)
                )

            elif self.stage_spec[i] == 'DM':
                  self.attns.append(
                  DAttentionBaseline(fmap_size, fmap_size, heads,
                  hc, n_groups, attn_drop, proj_drop,
                  stride, offset_range_factor, use_pe, dwc_pe,
                  no_off, fixed_pe, ksize, log_cpb)
                )
          #  elif self.stage_spec[i] == 'X':
          #      self.attns.append(
          #          nn.Conv2d(dim_embed, dim_embed, kernel_size=window_size, padding=window_size // 2, groups=dim_embed)
          #      )
            else:
                raise NotImplementedError(f'Spec: {stage_spec[i]} is not supported.')

            self.drop_path.append(DropPath(drop_path_rate[i]) if drop_path_rate[i] > 0.0 else nn.Identity())

    def forward(self, x):

        x = self.proj(x)

        for d in range(self.depths):

        #    if self.use_lpu:
        #        x0 = x
        #        x = self.local_perception_units[d](x.contiguous())
        #        x = x + x0
        #        x = x
            if self.stage_spec[d] == 'DM':
                x0 = x
                x1= self.layer_norms[2 * d](x)
                x1, pos, ref = self.attns[d](x1)
                #print(x1.shape)

                x1 = x1 + x0
                x0 = x1

                N = x1.size(2) * x1.size(3) # N = H * W

                x1 = self.layer_norms[2* d +1](x1).contiguous()
                #print(x1.shape)

                x1 = x1.view(x1.size(0), N, x1.size(1)) # Reshape to [B, N, C]
                x1 = self.mlps[d](x1, x0.size(2), x0.size(3)) # MIX-FFN (MissFormer)

                x = (x1.view(x0.size(0), x0.size(1), x0.size(2), x0.size(3)) + x0) #Reshape to [B, C, H , W]
                #print(x.shape)

            #else:
            #    x0 = x
            #    x, pos, ref = self.attns[d](self.layer_norms[2 * d](x))
            #    #x, pos, ref = self.attns[d](x)
            #    x = self.layer_scales[2 * d](x)
            #    x = self.drop_path[d](x) + x0
            #    x0 = x
            #    x = self.mlps[d](self.layer_norms[2 * d + 1](x))
            #    x = self.layer_scales[2 * d + 1](x)
            #    x = self.drop_path[d](x) + x0

        return x


class DATMiss(nn.Module):

    def __init__(self, img_size=224, patch_size=4, num_classes=1000, expansion=4,
                 dim_stem=96, dims=[96, 192, 384, 768], depths=[2, 2, 6, 2],
                 heads=[3, 6, 12, 24], heads_q=[6, 12, 24, 48],
                 window_sizes=[7, 7, 7, 7],
                 drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0,
                 strides=[-1,-1,-1,-1],
                 offset_range_factor=[1, 2, 3, 4],
                 stage_spec=[['DM', 'DM'], ['DM', 'DM'], ['DM', 'DM', 'DM', 'DM', 'DM', 'D'], ['DM', 'DM']],
                 groups=[-1, -1, 3, 6],
                 use_pes=[False, False, False, False],
                 dwc_pes=[False, False, False, False],
                 sr_ratios=[8, 4, 2, 1],
                 lower_lr_kvs={},
                 fixed_pes=[False, False, False, False],
                 no_offs=[False, False, False, False],
                 ns_per_pts=[4, 4, 4, 4],
                 use_dwc_mlps=[False, False, False, False],
                 use_conv_patches=False,
                 ksizes=[9, 7, 5, 3],
                 ksize_qnas=[3, 3, 3, 3],
                 nqs=[2, 2, 2, 2],
                 qna_activation='exp',
                 nat_ksizes=[3,3,3,3],
                 layer_scale_values=[-1,-1,-1,-1],
                 use_lpus=[False, False, False, False],
                 log_cpb=[False, False, False, False],
                 **kwargs):
        super().__init__()

        self.patch_proj = nn.Sequential(
            nn.Conv2d(3, dim_stem // 2, 3, patch_size // 2, 1),
            LayerNormProxy(dim_stem // 2),
            nn.GELU(),
            nn.Conv2d(dim_stem // 2, dim_stem, 3, patch_size // 2, 1),
            LayerNormProxy(dim_stem)
        ) if use_conv_patches else nn.Sequential(
            nn.Conv2d(3, dim_stem, patch_size, patch_size, 0),
            LayerNormProxy(dim_stem)
        )

        img_size = img_size // patch_size #Patching 224 // 4 == 56
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]

        self.stages = nn.ModuleList()
        for i in range(4):
            dim1 = dim_stem if i == 0 else dims[i - 1] * 2
            dim2 = dims[i]
            #dim2 = 96
            self.stages.append(
                TransformerStage(
                    img_size, window_sizes[i], ns_per_pts[i],
                    dim1, dim2, depths[i],
                    stage_spec[i], groups[i], use_pes[i],
                    sr_ratios[i], heads[i], heads_q[i], strides[i],
                    offset_range_factor[i],
                    dwc_pes[i], no_offs[i], fixed_pes[i],
                    attn_drop_rate, drop_rate, expansion, drop_rate,
                    dpr[sum(depths[:i]):sum(depths[:i + 1])], use_dwc_mlps[i],
                    ksizes[i], nat_ksizes[i], ksize_qnas[i], nqs[i],qna_activation,
                    layer_scale_values[i], use_lpus[i], log_cpb[i]
                )
            )
            img_size = img_size // 2

        self.down_projs = nn.ModuleList()
        for i in range(3):
            self.down_projs.append(
                nn.Sequential(
                    nn.Conv2d(dims[i], dims[i + 1], 3, 2, 1, bias=False),
                    LayerNormProxy(dims[i + 1])
                ) if use_conv_patches else nn.Sequential(
                    nn.Conv2d(dims[i], dims[i + 1], 2, 2, 0, bias=False),
                    LayerNormProxy(dims[i + 1])
                )
            )

        #self.cls_norm = LayerNormProxy(dims[-1])
        #self.cls_head = nn.Linear(dims[-1], num_classes)

      #  self.lower_lr_kvs = lower_lr_kvs

        self.reset_parameters()

    def reset_parameters(self):

        for m in self.parameters():
            if isinstance(m, (nn.Linear, nn.Conv2d)):
                nn.init.kaiming_normal_(m.weight)
                nn.init.zeros_(m.bias)

    @torch.no_grad()
    def load_pretrained(self, state_dict, lookup_22k):

        new_state_dict = {}
        for state_key, state_value in state_dict.items():
            keys = state_key.split('.')
            m = self
            for key in keys:
                if key.isdigit():
                    m = m[int(key)]
                else:
                    m = getattr(m, key)
            if m.shape == state_value.shape:
                new_state_dict[state_key] = state_value
            else:
                # Ignore different shapes
                if 'relative_position_index' in keys:
                    new_state_dict[state_key] = m.data
                if 'q_grid' in keys:
                    new_state_dict[state_key] = m.data
                if 'reference' in keys:
                    new_state_dict[state_key] = m.data
                # Bicubic Interpolation
                if 'relative_position_bias_table' in keys:
                    n, c = state_value.size()
                    l_side = int(math.sqrt(n))
                    assert n == l_side ** 2
                    L = int(math.sqrt(m.shape[0]))
                    pre_interp = state_value.reshape(1, l_side, l_side, c).permute(0, 3, 1, 2)
                    post_interp = F.interpolate(pre_interp, (L, L), mode='bicubic')
                    new_state_dict[state_key] = post_interp.reshape(c, L ** 2).permute(1, 0)
                if 'rpe_table' in keys:
                    c, h, w = state_value.size()
                    C, H, W = m.data.size()
                    pre_interp = state_value.unsqueeze(0)
                    post_interp = F.interpolate(pre_interp, (H, W), mode='bicubic')
                    new_state_dict[state_key] = post_interp.squeeze(0)
                if 'cls_head' in keys:
                    new_state_dict[state_key] = state_value[lookup_22k]

        msg = self.load_state_dict(new_state_dict, strict=False)
        return msg

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table', 'rpe_table'}

    def forward(self, x):
        x = self.patch_proj(x)
        skip_connection_input = []
        for i in range(4):
            x = self.stages[i](x)
            skip_connection_input.append(x)
            if i < 3:
                x = self.down_projs[i](x)
        #x = self.cls_norm(x)
        #x = F.adaptive_avg_pool2d(x, 1)
        #x = torch.flatten(x, 1)
        #x = self.cls_head(x)
        return x, skip_connection_input, None, None

class TransformerStage2(nn.Module):

    def __init__(self, fmap_size, window_size, ns_per_pt,
                 dim_in, dim_embed, depths, stage_spec, n_groups,
                 use_pe,sr_ratio,
                 heads, heads_q,
                 stride,
                 offset_range_factor,
                 dwc_pe, no_off, fixed_pe,
                 attn_drop, proj_drop, expansion, drop, drop_path_rate,
                 use_dwc_mlp, ksize,
                 nat_ksize,
                 k_qna,
                 nq_qna,
                 qna_activation,
                 layer_scale_value, use_lpu, log_cpb):

        super().__init__()
        fmap_size = to_2tuple(fmap_size)
        self.depths = depths
        hc = dim_embed // heads
        assert dim_embed == heads * hc
        self.proj = nn.Conv2d(dim_in, dim_embed, 1, 1, 0) if dim_in != dim_embed else nn.Identity()
        self.stage_spec = stage_spec
        self.use_lpu = use_lpu

        self.ln_cnvnxt = nn.ModuleDict(
            {str(d): LayerNormProxy(dim_embed) for d in range(depths) if stage_spec[d] == 'X'}
        )
        self.layer_norms = nn.ModuleList(
            [LayerNormProxy(dim_embed) if stage_spec[d // 2] != 'X' else nn.Identity() for d in range(2 * depths)]
        )

        mlp_fn = TransformerMLPWithConv if use_dwc_mlp else MixFFN_skip

        self.mlps = nn.ModuleList(
            [
                mlp_fn(dim_embed, expansion, drop) if use_dwc_mlp else mlp_fn(dim_embed, dim_embed*expansion)for _ in range(depths)
            ]
        )
        self.attns = nn.ModuleList()
        self.drop_path = nn.ModuleList()
        self.layer_scales = nn.ModuleList(
            [
                LayerScale(dim_embed, init_values=layer_scale_value) if layer_scale_value > 0.0 else nn.Identity()
                for _ in range(2 * depths)
            ]
        )
        self.local_perception_units = nn.ModuleList(
            [
                nn.Conv2d(dim_embed, dim_embed, kernel_size=3, stride=1, padding=1, groups=dim_embed) if use_lpu else nn.Identity()
                for _ in range(depths)
            ]
        )

        for i in range(self.depths):

            if stage_spec[i] == 'D':
                self.attns.append(
                    DAttentionBaseline(fmap_size, fmap_size, heads,
                    hc, n_groups, attn_drop, proj_drop,
                    stride, offset_range_factor, use_pe, dwc_pe,
                    no_off, fixed_pe, ksize, log_cpb)
                )

            elif self.stage_spec[i] == 'DM':
                  self.attns.append(
                  DAttentionBaseline(fmap_size, fmap_size, heads,
                  hc, n_groups, attn_drop, proj_drop,
                  stride, offset_range_factor, use_pe, dwc_pe,
                  no_off, fixed_pe, ksize, log_cpb)
                )
          #  elif self.stage_spec[i] == 'X':
          #      self.attns.append(
          #          nn.Conv2d(dim_embed, dim_embed, kernel_size=window_size, padding=window_size // 2, groups=dim_embed)
          #      )
            else:
                raise NotImplementedError(f'Spec: {stage_spec[i]} is not supported.')

            self.drop_path.append(DropPath(drop_path_rate[i]) if drop_path_rate[i] > 0.0 else nn.Identity())

    def forward(self, x):

        x = self.proj(x)

        for d in range(self.depths):

        #    if self.use_lpu:
        #        x0 = x
        #        x = self.local_perception_units[d](x.contiguous())
        #        x = x + x0
        #        x = x
            if self.stage_spec[d] == 'DM':
                x0 = x
                x1= self.layer_norms[2* d](x)
                x1, pos, ref = self.attns[d](x1)

                x1 = x1 + x0
                x0 = x1

                N = x1.size(2) * x1.size(3) # N = H * W

                x1 = self.layer_norms[2* d +1](x1).contiguous()

                x1 = x1.view(x1.size(0), N, x1.size(1)) # Reshape to [B, N, C]
                x1 = self.mlps[d](x1, x0.size(2), x0.size(3)) # MIX-FFN (MissFormer)

                x = x1.view(x0.size(0), x0.size(1), x0.size(2) , x0.size(3)) + x0 #Reshape to [B, C, H , W]

            #else:
            #    x0 = x
            #    x, pos, ref = self.attns[d](self.layer_norms[2 * d](x))
            #    #x, pos, ref = self.attns[d](x)
            #    x = self.layer_scales[2 * d](x)
            #    x = self.drop_path[d](x) + x0
            #    x0 = x
            #    x = self.mlps[d](self.layer_norms[2 * d + 1](x))
            #    x = self.layer_scales[2 * d + 1](x)
            #    x = self.drop_path[d](x) + x0

        return x


class DATMiss2(nn.Module):

    def __init__(self, img_size=224, patch_size=4, num_classes=1000, expansion=4,
                 dim_stem=96, dims=[96, 192, 384, 768], depths=[2, 2, 6, 2],
                 heads=[3, 6, 12, 24], heads_q=[6, 12, 24, 48],
                 window_sizes=[7, 7, 7, 7],
                 drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0,
                 strides=[-1,-1,-1,-1],
                 offset_range_factor=[1, 2, 3, 4],
                 stage_spec=[['DM', 'DM'], ['DM', 'DM'], ['DM', 'DM', 'DM', 'DM', 'DM', 'D'], ['DM', 'DM']],
                 groups=[-1, -1, 3, 6],
                 use_pes=[False, False, False, False],
                 dwc_pes=[False, False, False, False],
                 sr_ratios=[8, 4, 2, 1],
                 lower_lr_kvs={},
                 fixed_pes=[False, False, False, False],
                 no_offs=[False, False, False, False],
                 ns_per_pts=[4, 4, 4, 4],
                 use_dwc_mlps=[False, False, False, False],
                 use_conv_patches=False,
                 ksizes=[9, 7, 5, 3],
                 ksize_qnas=[3, 3, 3, 3],
                 nqs=[2, 2, 2, 2],
                 qna_activation='exp',
                 nat_ksizes=[3,3,3,3],
                 layer_scale_values=[-1,-1,-1,-1],
                 use_lpus=[False, False, False, False],
                 log_cpb=[False, False, False, False],
                 **kwargs):
        super().__init__()

        self.patch_proj = nn.Sequential(
            nn.Conv2d(3, dim_stem // 2, 3, patch_size // 2, 1),
            LayerNormProxy(dim_stem // 2),
            nn.GELU(),
            nn.Conv2d(dim_stem // 2, dim_stem, 3, patch_size // 2, 1),
            LayerNormProxy(dim_stem)
        ) if use_conv_patches else nn.Sequential(
            nn.Conv2d(3, dim_stem, patch_size, patch_size, 0),
            LayerNormProxy(dim_stem)
        )

        img_size = img_size // patch_size #Patching 224 // 4 == 56
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]

        self.stages = nn.ModuleList()
        for i in range(1):
            dim1 = dim_stem if i == 0 else dims[i - 1] * 2
            dim2 = dims[i]
            #dim2 = 96
            self.stages.append(
                TransformerStage2(
                    img_size, window_sizes[i], ns_per_pts[i],
                    dim1, dim2, depths[i],
                    stage_spec[i], groups[i], use_pes[i],
                    sr_ratios[i], heads[i], heads_q[i], strides[i],
                    offset_range_factor[i],
                    dwc_pes[i], no_offs[i], fixed_pes[i],
                    attn_drop_rate, drop_rate, expansion, drop_rate,
                    dpr[sum(depths[:i]):sum(depths[:i + 1])], use_dwc_mlps[i],
                    ksizes[i], nat_ksizes[i], ksize_qnas[i], nqs[i],qna_activation,
                    layer_scale_values[i], use_lpus[i], log_cpb[i]
                )
            )
            img_size = img_size // 2

        self.down_projs = nn.ModuleList()
#        for i in range(3):
#            self.down_projs.append(
#                nn.Sequential(
#                    nn.Conv2d(dims[i], dims[i + 1], 3, 2, 1, bias=False),
#                    LayerNormProxy(dims[i + 1])
#                ) if use_conv_patches else nn.Sequential(
#                    nn.Conv2d(dims[i], dims[i + 1], 2, 2, 0, bias=False),
#                    LayerNormProxy(dims[i + 1])
#                )
#            )

        #self.cls_norm = LayerNormProxy(dims[-1])
        #self.cls_head = nn.Linear(dims[-1], num_classes)

      #  self.lower_lr_kvs = lower_lr_kvs

        self.reset_parameters()

    def reset_parameters(self):

        for m in self.parameters():
            if isinstance(m, (nn.Linear, nn.Conv2d)):
                nn.init.kaiming_normal_(m.weight)
                nn.init.zeros_(m.bias)

    @torch.no_grad()
    def load_pretrained(self, state_dict, lookup_22k):

        new_state_dict = {}
        for state_key, state_value in state_dict.items():
            keys = state_key.split('.')
            m = self
            for key in keys:
                if key.isdigit():
                    m = m[int(key)]
                else:
                    m = getattr(m, key)
            if m.shape == state_value.shape:
                new_state_dict[state_key] = state_value
            else:
                # Ignore different shapes
                if 'relative_position_index' in keys:
                    new_state_dict[state_key] = m.data
                if 'q_grid' in keys:
                    new_state_dict[state_key] = m.data
                if 'reference' in keys:
                    new_state_dict[state_key] = m.data
                # Bicubic Interpolation
                if 'relative_position_bias_table' in keys:
                    n, c = state_value.size()
                    l_side = int(math.sqrt(n))
                    assert n == l_side ** 2
                    L = int(math.sqrt(m.shape[0]))
                    pre_interp = state_value.reshape(1, l_side, l_side, c).permute(0, 3, 1, 2)
                    post_interp = F.interpolate(pre_interp, (L, L), mode='bicubic')
                    new_state_dict[state_key] = post_interp.reshape(c, L ** 2).permute(1, 0)
                if 'rpe_table' in keys:
                    c, h, w = state_value.size()
                    C, H, W = m.data.size()
                    pre_interp = state_value.unsqueeze(0)
                    post_interp = F.interpolate(pre_interp, (H, W), mode='bicubic')
                    new_state_dict[state_key] = post_interp.squeeze(0)
                if 'cls_head' in keys:
                    new_state_dict[state_key] = state_value[lookup_22k]

        msg = self.load_state_dict(new_state_dict, strict=False)
        return msg

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table', 'rpe_table'}

    def forward(self, x):
        x = self.patch_proj(x)
        skip_connection_input = []
        for i in range(1):
            x = self.stages[i](x)
            skip_connection_input.append(x)
#            if i < 3:
#                x = self.down_projs[i](x)
        #x = self.cls_norm(x)
        #x = F.adaptive_avg_pool2d(x, 1)
        #x = torch.flatten(x, 1)
        #x = self.cls_head(x)
        return x, skip_connection_input, None, None


class TransformerStage3(nn.Module):

    def __init__(self, fmap_size, window_size, ns_per_pt,
                 dim_in, dim_embed, depths, stage_spec, n_groups,
                 use_pe, sr_ratio,
                 heads, heads_q,
                 stride,
                 offset_range_factor,
                 dwc_pe, no_off, fixed_pe,
                 attn_drop, proj_drop, expansion, drop, drop_path_rate,
                 use_dwc_mlp, ksize,
                 nat_ksize,
                 k_qna,
                 nq_qna,
                 qna_activation,
                 layer_scale_value, use_lpu, log_cpb):

        super().__init__()
        fmap_size = to_2tuple(fmap_size)
        self.depths = depths
        hc = dim_embed // heads
        assert dim_embed == heads * hc
        self.proj = nn.Conv2d(dim_in, dim_embed, 1, 1, 0) if dim_in != dim_embed else nn.Identity()
        self.stage_spec = stage_spec
        self.use_lpu = use_lpu

        self.ln_cnvnxt = nn.ModuleDict(
            {str(d): LayerNormProxy(dim_embed) for d in range(depths) if stage_spec[d] == 'X'}
        )
        self.layer_norms = nn.ModuleList(
            [LayerNormProxy(dim_embed) if stage_spec[d // 2] != 'X' else nn.Identity() for d in range(2 * depths)]
        )

        mlp_fn = TransformerMLPWithConv if use_dwc_mlp else MixFFN_skip

        self.mlps = nn.ModuleList(
            [
                mlp_fn(dim_embed, expansion, drop) if use_dwc_mlp else mlp_fn(dim_embed, dim_embed * expansion) for _ in
                range(depths)

            ]
        )
        self.attns = nn.ModuleList()
        self.drop_path = nn.ModuleList()
        self.layer_scales = nn.ModuleList(
            [
                LayerScale(dim_embed, init_values=layer_scale_value) if layer_scale_value > 0.0 else nn.Identity()
                for _ in range(2 * depths)
            ]
        )
        self.local_perception_units = nn.ModuleList(
            [
                nn.Conv2d(dim_embed, dim_embed, kernel_size=3, stride=1, padding=1,
                          groups=dim_embed) if use_lpu else nn.Identity()
                for _ in range(depths)
            ]
        )

        for i in range(self.depths):

            if stage_spec[i] == 'D':  # Simple deformable attn
                self.attns.append(
                    DAttentionBaseline(fmap_size, fmap_size, heads,
                                       hc, n_groups, attn_drop, proj_drop,
                                       stride, offset_range_factor, use_pe, dwc_pe,
                                       no_off, fixed_pe, ksize, log_cpb)
                )
            elif stage_spec[i] == 'L':
                self.attns.append(
                    LocalAttention(dim_embed, heads, window_size, attn_drop, proj_drop)
                )
            elif stage_spec[i] == 'S':
                shift_size = math.ceil(window_size / 2)
                self.attns.append(
                    ShiftWindowAttention(dim_embed, heads, window_size, attn_drop, proj_drop, shift_size, fmap_size)
                )

            elif self.stage_spec[i] == 'DM':  # Deformable attn + Mix-skip FFN
                self.attns.append(
                    DAttentionBaseline(fmap_size, fmap_size, heads,
                                       hc, n_groups, attn_drop, proj_drop,
                                       stride, offset_range_factor, use_pe, dwc_pe,
                                       no_off, fixed_pe, ksize, log_cpb)
                )
            elif self.stage_spec[i] == 'X':  # Without any attention module (just DWconv)
                self.attns.append(
                    nn.Conv2d(dim_embed, dim_embed, kernel_size=window_size, padding=window_size // 2, groups=dim_embed)
                )
            else:
                raise NotImplementedError(f'Spec: {stage_spec[i]} is not supported.')

            self.drop_path.append(DropPath(drop_path_rate[i]) if drop_path_rate[i] > 0.0 else nn.Identity())

    def forward(self, x):

        x = self.proj(x)

        for d in range(self.depths):

            if self.use_lpu:
                x0 = x
                x = self.local_perception_units[d](x.contiguous())
                x = x + x0
                x = x
            if self.stage_spec[d] == 'DM' or self.stage_spec[d] == 'L' or self.stage_spec[d] == 'S':
                # print(self.stage_spec[d])
                x0 = x
                x1 = self.layer_norms[2 * d](x)
                x1, pos, ref = self.attns[d](x1)

                x1 = x1 + x0
                x0 = x1

                N = x1.size(2) * x1.size(3)  # N = H * W

                x1 = self.layer_norms[2 * d + 1](x1).contiguous()

                x1 = x1.view(x1.size(0), N, x1.size(1))  # Reshape to [B, N, C]
                x1 = self.mlps[d](x1, x0.size(2), x0.size(3))  # MIX-FFN (MissFormer)

                x = x1.view(x0.size(0), x0.size(1), x0.size(2), x0.size(3)) + x0  # Reshape to [B, C, H , W]

            else:
                x0 = x
                x, pos, ref = self.attns[d](self.layer_norms[2 * d](x))
                # x, pos, ref = self.attns[d](x)
                x = self.layer_scales[2 * d](x)
                x = self.drop_path[d](x) + x0
                x0 = x
                x = self.mlps[d](self.layer_norms[2 * d + 1](x))
                x = self.layer_scales[2 * d + 1](x)
                x = self.drop_path[d](x) + x0

        return x


class DATMissLG(nn.Module):

    def __init__(self, img_size=224, patch_size=4, num_classes=1000, expansion=4,
                 dim_stem=96, dims=[96, 192, 384, 768], depths=[2, 2, 6, 2],
                 heads=[3, 6, 12, 24], heads_q=[6, 12, 24, 48],
                 window_sizes=[7, 7, 7, 7],
                 drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0,
                 strides=[-1, -1, -1, -1],
                 offset_range_factor=[1, 2, 3, 4],
                 stage_spec=[['L', 'DM'], ['L', 'DM'], ['L', 'DM', 'L', 'DM', 'L', 'DM'], ['DM', 'DM']],
                 groups=[-1, -1, 3, 6],
                 use_pes=[False, False, False, False],
                 dwc_pes=[False, False, False, False],
                 sr_ratios=[8, 4, 2, 1],
                 lower_lr_kvs={},
                 fixed_pes=[False, False, False, False],
                 no_offs=[False, False, False, False],
                 ns_per_pts=[4, 4, 4, 4],
                 use_dwc_mlps=[False, False, False, False],
                 use_conv_patches=False,
                 ksizes=[9, 7, 5, 3],
                 ksize_qnas=[3, 3, 3, 3],
                 nqs=[2, 2, 2, 2],
                 qna_activation='exp',
                 nat_ksizes=[3, 3, 3, 3],
                 layer_scale_values=[-1, -1, -1, -1],
                 use_lpus=[False, False, False, False],
                 log_cpb=[False, False, False, False],
                 **kwargs):
        super().__init__()

        self.patch_proj = nn.Sequential(
            nn.Conv2d(3, dim_stem // 2, 3, patch_size // 2, 1),
            LayerNormProxy(dim_stem // 2),
            nn.GELU(),
            nn.Conv2d(dim_stem // 2, dim_stem, 3, patch_size // 2, 1),
            LayerNormProxy(dim_stem)
        ) if use_conv_patches else nn.Sequential(
            nn.Conv2d(3, dim_stem, patch_size, patch_size, 0),
            LayerNormProxy(dim_stem)
        )

        img_size = img_size // patch_size  # Patching 224 // 4 == 56
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]

        self.stages = nn.ModuleList()
        for i in range(4):
            dim1 = dim_stem if i == 0 else dims[i - 1] * 2
            dim2 = dims[i]
            # dim2 = 96
            self.stages.append(
                TransformerStage3(
                    img_size, window_sizes[i], ns_per_pts[i],
                    dim1, dim2, depths[i],
                    stage_spec[i], groups[i], use_pes[i],
                    sr_ratios[i], heads[i], heads_q[i], strides[i],
                    offset_range_factor[i],
                    dwc_pes[i], no_offs[i], fixed_pes[i],
                    attn_drop_rate, drop_rate, expansion, drop_rate,
                    dpr[sum(depths[:i]):sum(depths[:i + 1])], use_dwc_mlps[i],
                    ksizes[i], nat_ksizes[i], ksize_qnas[i], nqs[i], qna_activation,
                    layer_scale_values[i], use_lpus[i], log_cpb[i]
                )
            )
            img_size = img_size // 2

        self.down_projs = nn.ModuleList()
        for i in range(3):
            self.down_projs.append(
                nn.Sequential(
                    nn.Conv2d(dims[i], dims[i + 1], 3, 2, 1, bias=False),
                    LayerNormProxy(dims[i + 1])
                ) if use_conv_patches else nn.Sequential(
                    nn.Conv2d(dims[i], dims[i + 1], 2, 2, 0, bias=False),
                    LayerNormProxy(dims[i + 1])
                )
            )

        # self.cls_norm = LayerNormProxy(dims[-1])
        # self.cls_head = nn.Linear(dims[-1], num_classes)

        #  self.lower_lr_kvs = lower_lr_kvs

        self.reset_parameters()

    def reset_parameters(self):

        for m in self.parameters():
            if isinstance(m, (nn.Linear, nn.Conv2d)):
                nn.init.kaiming_normal_(m.weight)
                nn.init.zeros_(m.bias)

    @torch.no_grad()
    def load_pretrained(self, state_dict, lookup_22k):

        new_state_dict = {}
        for state_key, state_value in state_dict.items():
            keys = state_key.split('.')
            m = self
            for key in keys:
                if key.isdigit():
                    m = m[int(key)]
                else:
                    m = getattr(m, key)
            if m.shape == state_value.shape:
                new_state_dict[state_key] = state_value
            else:
                # Ignore different shapes
                if 'relative_position_index' in keys:
                    new_state_dict[state_key] = m.data
                if 'q_grid' in keys:
                    new_state_dict[state_key] = m.data
                if 'reference' in keys:
                    new_state_dict[state_key] = m.data
                # Bicubic Interpolation
                if 'relative_position_bias_table' in keys:
                    n, c = state_value.size()
                    l_side = int(math.sqrt(n))
                    assert n == l_side ** 2
                    L = int(math.sqrt(m.shape[0]))
                    pre_interp = state_value.reshape(1, l_side, l_side, c).permute(0, 3, 1, 2)
                    post_interp = F.interpolate(pre_interp, (L, L), mode='bicubic')
                    new_state_dict[state_key] = post_interp.reshape(c, L ** 2).permute(1, 0)
                if 'rpe_table' in keys:
                    c, h, w = state_value.size()
                    C, H, W = m.data.size()
                    pre_interp = state_value.unsqueeze(0)
                    post_interp = F.interpolate(pre_interp, (H, W), mode='bicubic')
                    new_state_dict[state_key] = post_interp.squeeze(0)
                if 'cls_head' in keys:
                    new_state_dict[state_key] = state_value[lookup_22k]

        msg = self.load_state_dict(new_state_dict, strict=False)
        return msg

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table', 'rpe_table'}

    def forward(self, x):
        x = self.patch_proj(x)
        skip_connection_input = []
        for i in range(4):
            x = self.stages[i](x)
            skip_connection_input.append(x)
            if i < 3:
                x = self.down_projs[i](x)
        # x = self.cls_norm(x)
        # x = F.adaptive_avg_pool2d(x, 1)
        # x = torch.flatten(x, 1)
        # x = self.cls_head(x)
        return x, skip_connection_input, None, None

class DATMissLG2(nn.Module):

    def __init__(self, img_size=224, patch_size=4, num_classes=1000, expansion=4,
                 dim_stem=96, dims=[96, 192, 384, 768], depths=[2, 2, 6, 2],
                 heads=[3, 6, 12, 24], heads_q=[6, 12, 24, 48],
                 window_sizes=[7, 7, 7, 7],
                 drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0,
                 strides=[-1, -1, -1, -1],
                 offset_range_factor=[1, 2, 3, 4],
                 stage_spec=[['L', 'L'], ['L', 'L'], ['L', 'DM', 'L', 'DM', 'L', 'DM'], ['DM', 'DM']],
                 groups=[-1, -1, 3, 6],
                 use_pes=[False, False, False, False],
                 dwc_pes=[False, False, False, False],
                 sr_ratios=[8, 4, 2, 1],
                 lower_lr_kvs={},
                 fixed_pes=[False, False, False, False],
                 no_offs=[False, False, False, False],
                 ns_per_pts=[4, 4, 4, 4],
                 use_dwc_mlps=[False, False, False, False],
                 use_conv_patches=False,
                 ksizes=[9, 7, 5, 3],
                 ksize_qnas=[3, 3, 3, 3],
                 nqs=[2, 2, 2, 2],
                 qna_activation='exp',
                 nat_ksizes=[3, 3, 3, 3],
                 layer_scale_values=[-1, -1, -1, -1],
                 use_lpus=[False, False, False, False],
                 log_cpb=[False, False, False, False],
                 **kwargs):
        super().__init__()

        self.patch_proj = nn.Sequential(
            nn.Conv2d(3, dim_stem // 2, 3, patch_size // 2, 1),
            LayerNormProxy(dim_stem // 2),
            nn.GELU(),
            nn.Conv2d(dim_stem // 2, dim_stem, 3, patch_size // 2, 1),
            LayerNormProxy(dim_stem)
        ) if use_conv_patches else nn.Sequential(
            nn.Conv2d(3, dim_stem, patch_size, patch_size, 0),
            LayerNormProxy(dim_stem)
        )

        img_size = img_size // patch_size  # Patching 224 // 4 == 56
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]

        self.stages = nn.ModuleList()
        for i in range(4):
            dim1 = dim_stem if i == 0 else dims[i - 1] * 2
            dim2 = dims[i]
            # dim2 = 96
            self.stages.append(
                TransformerStage3(
                    img_size, window_sizes[i], ns_per_pts[i],
                    dim1, dim2, depths[i],
                    stage_spec[i], groups[i], use_pes[i],
                    sr_ratios[i], heads[i], heads_q[i], strides[i],
                    offset_range_factor[i],
                    dwc_pes[i], no_offs[i], fixed_pes[i],
                    attn_drop_rate, drop_rate, expansion, drop_rate,
                    dpr[sum(depths[:i]):sum(depths[:i + 1])], use_dwc_mlps[i],
                    ksizes[i], nat_ksizes[i], ksize_qnas[i], nqs[i], qna_activation,
                    layer_scale_values[i], use_lpus[i], log_cpb[i]
                )
            )
            img_size = img_size // 2

        self.down_projs = nn.ModuleList()
        for i in range(3):
            self.down_projs.append(
                nn.Sequential(
                    nn.Conv2d(dims[i], dims[i + 1], 3, 2, 1, bias=False),
                    LayerNormProxy(dims[i + 1])
                ) if use_conv_patches else nn.Sequential(
                    nn.Conv2d(dims[i], dims[i + 1], 2, 2, 0, bias=False),
                    LayerNormProxy(dims[i + 1])
                )
            )

        # self.cls_norm = LayerNormProxy(dims[-1])
        # self.cls_head = nn.Linear(dims[-1], num_classes)

        #  self.lower_lr_kvs = lower_lr_kvs

        self.reset_parameters()

    def reset_parameters(self):

        for m in self.parameters():
            if isinstance(m, (nn.Linear, nn.Conv2d)):
                nn.init.kaiming_normal_(m.weight)
                nn.init.zeros_(m.bias)

    @torch.no_grad()
    def load_pretrained(self, state_dict, lookup_22k):

        new_state_dict = {}
        for state_key, state_value in state_dict.items():
            keys = state_key.split('.')
            m = self
            for key in keys:
                if key.isdigit():
                    m = m[int(key)]
                else:
                    m = getattr(m, key)
            if m.shape == state_value.shape:
                new_state_dict[state_key] = state_value
            else:
                # Ignore different shapes
                if 'relative_position_index' in keys:
                    new_state_dict[state_key] = m.data
                if 'q_grid' in keys:
                    new_state_dict[state_key] = m.data
                if 'reference' in keys:
                    new_state_dict[state_key] = m.data
                # Bicubic Interpolation
                if 'relative_position_bias_table' in keys:
                    n, c = state_value.size()
                    l_side = int(math.sqrt(n))
                    assert n == l_side ** 2
                    L = int(math.sqrt(m.shape[0]))
                    pre_interp = state_value.reshape(1, l_side, l_side, c).permute(0, 3, 1, 2)
                    post_interp = F.interpolate(pre_interp, (L, L), mode='bicubic')
                    new_state_dict[state_key] = post_interp.reshape(c, L ** 2).permute(1, 0)
                if 'rpe_table' in keys:
                    c, h, w = state_value.size()
                    C, H, W = m.data.size()
                    pre_interp = state_value.unsqueeze(0)
                    post_interp = F.interpolate(pre_interp, (H, W), mode='bicubic')
                    new_state_dict[state_key] = post_interp.squeeze(0)
                if 'cls_head' in keys:
                    new_state_dict[state_key] = state_value[lookup_22k]

        msg = self.load_state_dict(new_state_dict, strict=False)
        return msg

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table', 'rpe_table'}

    def forward(self, x):
        x = self.patch_proj(x)
        skip_connection_input = []
        for i in range(4):
            x = self.stages[i](x)
            skip_connection_input.append(x)
            if i < 3:
                x = self.down_projs[i](x)
        # x = self.cls_norm(x)
        # x = F.adaptive_avg_pool2d(x, 1)
        # x = torch.flatten(x, 1)
        # x = self.cls_head(x)
        return x, skip_connection_input, None, None

if __name__ == "__main__":

    x_test2 = torch.randn(1, 3, 224, 224)
    my_test_model2 = DATMiss(img_size=224, patch_size=4, expansion=4, dim_stem=96,
                             dims=[96, 192, 384, 768], depths=[2, 2, 2, 2],
                             heads=[3, 6, 12, 24],
                             window_sizes=[7, 7, 7, 7],
                             drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0,
                             strides=[1, 1, 1, 1],
                             offset_range_factor=[1, 2, 3, 4],
                             stage_spec=[['DM', "DM"], ['DM', "DM"], ['DM', "DM"], ['DM', "DM"]],
                             groups=[1, 1, 3, 6],
                             use_pes=[False, False, False, False],
                             dwc_pes=[False, False, False, False],
                             sr_ratios=[8, 4, 2, 1],
                             lower_lr_kvs={},
                             fixed_pes=[False, False, False, False],
                             no_offs=[False, False, False, False],
                             ns_per_pts=[4, 4, 4, 4],
                             use_dwc_mlps=[False, False, False, False],
                             use_conv_patches=True,
                             ksizes=[9, 7, 5, 3],
                             ksize_qnas=[3, 3, 3, 3],
                             nqs=[2, 2, 2, 2],
                             qna_activation='exp',
                             nat_ksizes=[3, 3, 3, 3],
                             layer_scale_values=[-1, -1, -1, -1],
                             use_lpus=[False, False, False, False],
                             log_cpb=[False, False, False, False])

    out_put = my_test_model2(x_test2)
    print(out_put[0].shape)#
          #out_put[1][0].shape, out_put[1][1].shape, out_put[1][2].shape, out_put[1][3].shape)
    print('----------------')
    x_test3 = torch.randn(1, 3, 224, 224)
    my_test_model3 = DATMiss2(img_size=224, patch_size=4, expansion=4, dim_stem=96,
                             dims=[96, 192, 384, 768], depths=[2, 2, 2, 2],
                             heads=[3, 6, 12, 24],
                             window_sizes=[7, 7, 7, 7],
                             drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0,
                             strides=[1, 1, 1, 1],
                             offset_range_factor=[1, 2, 3, 4],
                             stage_spec=[['DM', "DM"], ['DM', "DM"], ['DM', "DM"], ['DM', "DM"]],
                             groups=[1, 1, 3, 6],
                             use_pes=[False, False, False, False],
                             dwc_pes=[False, False, False, False],
                             sr_ratios=[8, 4, 2, 1],
                             lower_lr_kvs={},
                             fixed_pes=[False, False, False, False],
                             no_offs=[False, False, False, False],
                             ns_per_pts=[4, 4, 4, 4],
                             use_dwc_mlps=[False, False, False, False],
                             use_conv_patches=True,
                             ksizes=[9, 7, 5, 3],
                             ksize_qnas=[3, 3, 3, 3],
                             nqs=[2, 2, 2, 2],
                             qna_activation='exp',
                             nat_ksizes=[3, 3, 3, 3],
                             layer_scale_values=[-1, -1, -1, -1],
                             use_lpus=[False, False, False, False],
                             log_cpb=[False, False, False, False])

    out_put2 = my_test_model3(x_test3)
    print('output 2')
    print(out_put2[0].shape, out_put2[1][0].shape)
    print('----------------')
    print('output 3')
    x_test4 = torch.randn(1, 3, 224, 224)
    my_test_model2 = DATMissLG(img_size=224, patch_size=4, expansion=4, dim_stem=96,
                               dims=[96, 192, 384, 768], depths=[2, 2, 2, 2],
                               heads=[3, 6, 12, 24],
                               window_sizes=[7, 7, 7, 7],
                               drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0,
                               strides=[1, 1, 1, 1],
                               offset_range_factor=[1, 2, 3, 4],
                               stage_spec=[['L', "DM"], ['L', "DM"], ['L', "DM", 'L', 'DM', "L", 'DM'], ['DM', "DM"]],
                               groups=[1, 1, 3, 6],
                               use_pes=[False, False, False, False],
                               dwc_pes=[False, False, False, False],
                               sr_ratios=[8, 4, 2, 1],
                               lower_lr_kvs={},
                               fixed_pes=[False, False, False, False],
                               no_offs=[False, False, False, False],
                               ns_per_pts=[4, 4, 4, 4],
                               use_dwc_mlps=[False, False, False, False],
                               use_conv_patches=True,
                               ksizes=[9, 7, 5, 3],
                               ksize_qnas=[3, 3, 3, 3],
                               nqs=[2, 2, 2, 2],
                               qna_activation='exp',
                               nat_ksizes=[3, 3, 3, 3],
                               layer_scale_values=[-1, -1, -1, -1],
                               use_lpus=[False, False, False, False],
                               log_cpb=[False, False, False, False])
    out_put3 = my_test_model2(x_test4)
    print(out_put3[0].shape, out_put3[1][0].shape)
    print('----------------')
    x_test5 = torch.randn(1, 3, 224, 224)
    my_test_model3 = DATMissLG2(img_size=224, patch_size=4, num_classes=1000, expansion=4,
                 dim_stem=96, dims=[96, 192, 384, 768], depths=[2, 2, 6, 2],
                 heads=[3, 6, 12, 24], heads_q=[6, 12, 24, 48],
                 window_sizes=[7, 7, 7, 7],
                 drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0,
                 strides=[1, 1, 1, 1],
                 offset_range_factor=[1, 2, 3, 4],
                 stage_spec=[['L', 'S'], ['L', 'S'], ['L', 'DM', 'L', 'DM', 'L', 'DM'], ['DM', 'DM']],
                 groups=[-1, -1, 3, 6],
                 use_pes=[False, False, False, False],
                 dwc_pes=[False, False, False, False],
                 sr_ratios=[8, 4, 2, 1],
                 lower_lr_kvs={},
                 fixed_pes=[False, False, False, False],
                 no_offs=[False, False, False, False],
                 ns_per_pts=[4, 4, 4, 4],
                 use_dwc_mlps=[False, False, False, False],
                 use_conv_patches=False,
                 ksizes=[9, 7, 5, 3],
                 ksize_qnas=[3, 3, 3, 3],
                 nqs=[2, 2, 2, 2],
                 qna_activation='exp',
                 nat_ksizes=[3, 3, 3, 3],
                 layer_scale_values=[-1, -1, -1, -1],
                 use_lpus=[False, False, False, False],
                 log_cpb=[False, False, False, False])
    out_put4 = my_test_model3(x_test5)
    print(out_put4[0].shape, out_put4[1][0].shape)
    print('output 4')
    print('----------------')