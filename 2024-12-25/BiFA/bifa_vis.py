# ---------------------------------------------------------------
# Copyright (c) 2021, NVIDIA Corporation. All rights reserved.
#
# This work is licensed under the NVIDIA Source Code License
# ---------------------------------------------------------------
from functools import partial
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from mmseg.utils import get_root_logger
from mmcv.runner import load_checkpoint
from models.seghead.segformer_head import SegFormerHead, SegFormerHeadz, SegFormerHead2
import math
from models.bifa_help.ImplicitFunction import fpn_ifa
from models.backbone.my_transformer import *
from models.ms_cam import MS_CAM


class flowmlp(nn.Module):
    def __init__(self, inplane):
        super(flowmlp, self).__init__()
        self.dwconv = nn.Conv2d(inplane*4, inplane*4, 3, 1, 1, bias=True, groups=inplane)
        self.Conv_enlarge = nn.Conv2d(inplane, inplane*4, 1)
        self.Conv_shrink = nn.Conv2d(inplane*4, inplane, 1)
        self.gelu = nn.GELU()

    def forward(self, x):
        x = self.Conv_enlarge(x)
        x = self.dwconv(x)
        x = self.gelu(x)
        x = self.Conv_shrink(x)

        return x

class DiffFlowN(nn.Module):
    def __init__(self, inplane, h, w):
        """
        implementation of diffflow
        :param inplane:
        :param norm_layer:
        """
        super(DiffFlowN, self).__init__()
        #self.ca1 = CA_Block(inplane, h, w)
        #self.ca2 = CA_Block(inplane, h, w)
        #self.Conv1 = DeformConv2d(inc=inplane, outc=inplane, kernel_size=1, padding=0, stride=1, bias=False)
        #self.Conv2 = DeformConv2d(inc=inplane, outc=inplane, kernel_size=1, padding=0, stride=1, bias=False)
        # self.dwconv = nn.Conv2d(inplane, inplane, 3, 1, 1, bias=True, groups=inplane)
        # self.Conv1=nn.Conv2d(inplane, inplane,1)
        # self.Conv2 = nn.Conv2d(inplane, inplane, 1)
        self.flowmlp1 = flowmlp(inplane)
        self.flowmlp2 = flowmlp(inplane)
        self.flow_make1 = nn.Conv2d(inplane *2 , 2, kernel_size=3, padding=1, bias=False)

    def forward(self, x1, x2):

        x1 = self.flowmlp1(x1)
        x2 = self.flowmlp2(x2)

        size = x1.size()[2:]
        flow1 = self.flow_make1(torch.cat([x1, x2], dim=1))


        seg_flow_warp1 = self.flow_warp(x1, flow1, size) #A
        diff1 = torch.abs(seg_flow_warp1 - x2)

        return diff1

    def flow_warp(self, input, flow, size):
        out_h, out_w = size
        n, c, h, w = input.size()

        norm = torch.tensor([[[[out_w, out_h]]]]).type_as(input).to(input.device)
        # new
        h_grid = torch.linspace(-1.0, 1.0, out_h).view(-1, 1).repeat(1, out_w)
        w_gird = torch.linspace(-1.0, 1.0, out_w).repeat(out_h, 1)

        grid = torch.cat((w_gird.unsqueeze(2), h_grid.unsqueeze(2)), 2)
        grid = grid.repeat(n, 1, 1, 1).type_as(input).to(input.device)
        grid = grid + flow.permute(0, 2, 3, 1) / norm

        output = F.grid_sample(input, grid)
        return output


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.dwconv = DWConv(hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W):
        x = self.fc1(x)
        x = self.dwconv(x, H, W)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., sr_ratio=1):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.cond = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.sigmoid = nn.Sigmoid()
        self.avgpool = nn.AvgPool2d(kernel_size=sr_ratio, stride=sr_ratio)
        self.avgpoolchannel = nn.AdaptiveAvgPool2d(1)


        self.sr_ratio = sr_ratio
        if sr_ratio > 1:
            self.sr = nn.Conv2d(dim, dim, kernel_size=sr_ratio, stride=sr_ratio)
            self.norm = nn.LayerNorm(dim)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W, cond):
        B, N, C = x.shape
        # print("self.dim is", self.dim)
        q = self.q(x)
        cond = self.cond(cond)
        cond_z = cond.permute(0, 2, 1).reshape(B, C, H, W)
        #cond_z = self.avgpoolchannel(cond_z)
        cond_score = self.sigmoid(cond_z)
        #print(cond_score.shape)

        # print("q is", q.shape)
        # q_conv = self.convshink(q)
        # q_conv = self.ln1(q_conv)
        # q_conv = self.relu1(q_conv)
        # q_conv = self.conv1(q_conv)
        # q_conv = self.ln2(q_conv)
        # q_conv = self.relu2(q_conv)
        # q_conv = self.conv2(q_conv)
        # q_conv = self.ln3(q_conv)
        # q_conv = self.relu3(q_conv)
        # q_conv = self.convenlarge(q_conv)
        # q_conv_score = self.sigmoid(q_conv)
        # # print("q_conv shape is", q_conv.shape)
        q = q.reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)

        cond_score = cond_score.reshape(B, -1, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        # print("q shape is", q.shape)
        # print("cond_score",cond_score.shape)

        if self.sr_ratio > 1:
            x_ = x.permute(0, 2, 1).reshape(B, C, H, W)
            # print("x_ shape is", x_.shape)
            cond_score_ = cond.permute(0, 2, 1).reshape(B, C, H, W)
            # print("cond_score_1", cond_score_.shape)
            #cond_score_k = self.avgpoolchannel(cond_score_)
            cond_score_k = self.sigmoid(cond_score_)
            x_ = self.sr(x_) #将x_ H W缩小self.sr_ratio倍 [B, C, H, W]-> [B, C, 8, 8]
            #cond_score_ = self.avgpool(cond_score_k)
            # print("cond_score_2", cond_score_.shape)
            # print("x_ shape is", x_.shape)
            x_ = x_.reshape(B, C, -1).permute(0, 2, 1)
            cond_score_ = cond_score_k.reshape(B, C, -1).permute(0, 2, 1)
            # print("cond_score_3", cond_score_.shape)
            x_ = self.norm(x_)
            kv = self.kv(x_).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            cond_score_ = cond_score_.reshape(B, -1, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
            # print("cond_score_4", cond_score_.shape)
            # print("x_ shape is", x_.shape)
        else:
            kv = self.kv(x).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            cond_score_ = cond_score
            # print("cond_score_ shape is cond_score")
        k, v = kv[0], kv[1]
        # print("v shape is", v.shape)

        # q_cond = q * cond_score_
        # print("q_cond", q_cond.shape)
        # v_cond = v * cond_score_
        # print("k_cond", k_cond.shape)

        attn = (q @ k.transpose(-2, -1)) * self.scale

        #添加AiA模块，进一步进行feature的判别
        # corr_map = attn
        # # print("corr_map ",corr_map.shape)
        # qkv_aia = corr_map.reshape(B, N, -1)
        # # print("qk_aia shape ", qkv_aia.shape)
        # # print(self.dim)
        # qk_aia = self.aia_linear(qkv_aia)
        # # print("qk_aia shape is", qk_aia.shape)
        # qk_aia = self.ln1_aia(qk_aia)
        # v_aia = self.ln2_aia(qkv_aia)
        # q_aia = self.qaia_linear(qk_aia)
        # k_aia = self.kaia_linear(qk_aia)
        # corr_attn = (q_aia @ k_aia.transpose(-2, -1)) * self.scale
        # corr_attn = corr_attn.softmax(dim=-1)
        # corr_v = (corr_attn @ v_aia)
        # corr_v_1 = self.mlp_linear(corr_v)
        # corr_attn_map = corr_v + corr_v_1
        # corr_attn_map = corr_attn_map.reshape(B, N, self.num_heads, self.dim *2 // self.num_heads).permute(0, 2, 1, 3)
        # # print("corr_attn_map shape",corr_attn_map.shape)
        #
        # attn = corr_attn_map + attn



        # print("attn shape is", attn.shape)
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        # x = x * q_conv_score
        # print("x shape is", x.shape)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x

class Attention_cond(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., sr_ratio=1):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.cond = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.sigmoid = nn.Sigmoid()

        self.sr_ratio = sr_ratio
        if sr_ratio > 1:
            self.sr = nn.Conv2d(dim, dim, kernel_size=sr_ratio, stride=sr_ratio)
            self.norm = nn.LayerNorm(dim)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W, cond):
        B, N, C = x.shape
        q = self.q(x)
        cond = self.cond(cond)
        cond_q = cond.permute(0, 2, 1).reshape(B, C, H, W)
        cond_q = self.sigmoid(cond_q)
        q = q.reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)

        cond_q = cond_q.reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)

        if self.sr_ratio > 1:
            x_ = x.permute(0, 2, 1).reshape(B, C, H, W)
            cond_k = cond.permute(0, 2, 1).reshape(B, C, H, W)
            x_ = self.sr(x_) #将x_ H W缩小self.sr_ratio倍 [B, C, H, W]-> [B, C, 8, 8]
            cond_k = self.sr(cond_k)
            x_ = x_.reshape(B, C, -1).permute(0, 2, 1)
            cond_k = cond_k.reshape(B, C, -1).permute(0, 2, 1)
            cond_k = self.sigmoid(cond_k)
            # print("cond_score_3", cond_score_.shape)
            x_ = self.norm(x_)
            cond_k = self.norm(cond_k)
            kv = self.kv(x_).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            cond_k = cond_k.reshape(B, -1, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
            # print("cond_score_4", cond_score_.shape)
            # print("x_ shape is", x_.shape)
        else:
            kv = self.kv(x).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            cond_k = cond_q
            # print("cond_score_ shape is cond_score")
        k, v = kv[0], kv[1]
        # print("v shape is", v.shape)

        q_cond = q * cond_q
        # print("q_cond", q_cond.shape)
        v_cond = v * cond_k
        # print("k_cond", k_cond.shape)

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v_cond).transpose(1, 2).reshape(B, N, C)
        # x = x * q_conv_score
        # print("x shape is", x.shape)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x

class Attentionz(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., sr_ratio=1):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.cond = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.sigmoid = nn.Sigmoid()
        self.norm1 = nn.LayerNorm(dim)
        self.norm3 = nn.LayerNorm(dim)



        self.sr_ratio = sr_ratio
        if sr_ratio > 1:
            self.sr = nn.Conv2d(dim, dim, kernel_size=sr_ratio, stride=sr_ratio)
            self.norm = nn.LayerNorm(dim)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W, cond):
        B, N, C = x.shape
        q = self.q(x)
        q = q.reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        if self.sr_ratio > 1:
            x_ = cond.permute(0, 2, 1).reshape(B, C, H, W)
            x_ = self.sr(x_) #将x_ H W缩小self.sr_ratio倍 [B, C, H, W]-> [B, C, 8, 8]
            x_ = x_.reshape(B, C, -1).permute(0, 2, 1)
            x_ = self.norm(x_)
            kv = self.kv(x_).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        else:
            kv = self.kv(cond).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        k, v = kv[0], kv[1]
        # print("v shape is", v.shape)

        attn = (q @ k.transpose(-2, -1)) * self.scale

        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

#没有实现对的cross_channel(结果达到了90.049)
class Attentionchannel(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., sr_ratio=1):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.cond = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.sigmoid = nn.Sigmoid()
        self.norm1 = nn.LayerNorm(dim)
        self.norm3 = nn.LayerNorm(dim)

        self.sr_ratio = sr_ratio
        if sr_ratio > 0:
            self.sr = nn.Conv2d(dim, dim, kernel_size=sr_ratio, stride=sr_ratio)
            self.norm = nn.LayerNorm(dim)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W, cond):
        #[B C H W] -> [B C N] ->[B N C]
        x = x.flatten(2)
        # x = self.norm1(x)
        B, N, C = x.shape
        # print("self.dim is", self.dim)
        q = self.q(x)
        q = q.reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 3, 1)
        # print("q shape",q.shape)
        if self.sr_ratio > 1: #[8, 4, 2, 1]
            # print("cond shape ", cond.shape)
            x_ = cond.permute(0, 2, 1).reshape(B, C, H, W)
            x_ = x_.reshape(B, C, -1).permute(0, 2, 1)
            x_ = self.norm(x_)
            kv = self.kv(x_).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 4, 1)
        else:
            kv = self.kv(x).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 4, 1)
        k, v = kv[0], kv[1]
        # print("v shape is", v.shape)

        attn = (q @ k.transpose(-2, -1)) * self.scale
        # print("attn ", attn.shape)

        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).reshape(B, C, N).transpose(1, 2)
        # print("attn_x shape", x.shape)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x

#实现对的cross_channel(结果达到了90.017)结果几乎一样
class AttentionRealCrossChannel(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., sr_ratio=2):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.cond = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x, H, W, cond):
        B, N, C = x.shape
        q = self.q(x)
        q = q.reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 3, 1)
        kv = self.kv(cond).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 4, 1)
        k, v = kv[0], kv[1]

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).reshape(B, C, N).transpose(1, 2)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x

class AttentionSelfChannel(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., sr_ratio=2):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.cond = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x, H, W, cond):
        B, N, C = x.shape
        q = self.q(x)
        q = q.reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 3, 1)
        kv = self.kv(x).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 4, 1)
        k, v = kv[0], kv[1]

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).reshape(B, C, N).transpose(1, 2)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, sr_ratio=1):
        super().__init__()
        self.sr_ratio = sr_ratio
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim,
            num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, sr_ratio=sr_ratio)
        self.attnz = Attentionz(
            dim,
            num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, sr_ratio=sr_ratio)
        self.attn_cond = Attention_cond(
            dim,
            num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, sr_ratio=sr_ratio)
        self.attn_channel = Attentionchannel(
            dim,
            num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, sr_ratio=sr_ratio)
        self.attn_realchannel = AttentionRealCrossChannel(
            dim,
            num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, sr_ratio=sr_ratio)
        self.attn_selfchannel = AttentionSelfChannel(
            dim,
            num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, sr_ratio=sr_ratio)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.sigmoid = nn.Sigmoid()

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x, H, W, cond):


        x = x + self.drop_path(self.attn(self.norm1(x), H, W, cond))
        x = x + self.drop_path(self.attn_realchannel(self.norm1(x), H, W, self.norm1(cond)))
        x = x + self.drop_path(self.mlp(self.norm2(x), H, W)) #[B, N, C]


        return x

class OverlapPatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """

    def __init__(self, img_size=256, patch_size=7, stride=4, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)

        self.img_size = img_size
        self.patch_size = patch_size
        self.H, self.W = img_size[0] // patch_size[0], img_size[1] // patch_size[1]
        self.num_patches = self.H * self.W
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=stride,
                              padding=(patch_size[0] // 2, patch_size[1] // 2))
        self.norm = nn.LayerNorm(embed_dim)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def forward(self, x):
        # print(self.img_size)
        x = self.proj(x)
        _, _, H, W = x.shape
        # print("H is ", H)
        x = x.flatten(2).transpose(1, 2)
        # print("x shape is ", x.shape)
        x = self.norm(x)

        return x, H, W


class MixVisionTransformer(nn.Module):
    def __init__(self, img_size=256, patch_size=16, in_chans=3, num_classes=1000, embed_dims=[64, 128, 256, 512],
                 num_heads=[1, 2, 4, 8], mlp_ratios=[4, 4, 4, 4], qkv_bias=False, qk_scale=None, drop_rate=0.,
                 attn_drop_rate=0., drop_path_rate=0., norm_layer=nn.LayerNorm,
                 depths=[3, 4, 6, 3], sr_ratios=[8, 4, 2, 1]):
        super().__init__()
        self.num_classes = num_classes
        self.depths = depths

        # patch_embed
        self.patch_embed1 = OverlapPatchEmbed(img_size=img_size, patch_size=7, stride=4, in_chans=in_chans,
                                              embed_dim=embed_dims[0])
        self.patch_embed2 = OverlapPatchEmbed(img_size=img_size // 4, patch_size=3, stride=2, in_chans=embed_dims[0],
                                              embed_dim=embed_dims[1])
        self.patch_embed3 = OverlapPatchEmbed(img_size=img_size // 8, patch_size=3, stride=2, in_chans=embed_dims[1],
                                              embed_dim=embed_dims[2])
        self.patch_embed4 = OverlapPatchEmbed(img_size=img_size // 16, patch_size=3, stride=2, in_chans=embed_dims[2],
                                              embed_dim=embed_dims[3])

        # transformer encoder
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule
        cur = 0
        self.block1 = nn.ModuleList([Block(
            dim=embed_dims[0], num_heads=num_heads[0], mlp_ratio=mlp_ratios[0], qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i], norm_layer=norm_layer,
            sr_ratio=sr_ratios[0])
            for i in range(depths[0])])
        self.norm1 = norm_layer(embed_dims[0])

        cur += depths[0]
        self.block2 = nn.ModuleList([Block(
            dim=embed_dims[1], num_heads=num_heads[1], mlp_ratio=mlp_ratios[1], qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i], norm_layer=norm_layer,
            sr_ratio=sr_ratios[1])
            for i in range(depths[1])])
        self.norm2 = norm_layer(embed_dims[1])

        cur += depths[1]
        self.block3 = nn.ModuleList([Block(
            dim=embed_dims[2], num_heads=num_heads[2], mlp_ratio=mlp_ratios[2], qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i], norm_layer=norm_layer,
            sr_ratio=sr_ratios[2])
            for i in range(depths[2])])
        self.norm3 = norm_layer(embed_dims[2])

        cur += depths[2]
        self.block4 = nn.ModuleList([Block(
            dim=embed_dims[3], num_heads=num_heads[3], mlp_ratio=mlp_ratios[3], qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur + i], norm_layer=norm_layer,
            sr_ratio=sr_ratios[3])
            for i in range(depths[3])])
        self.norm4 = norm_layer(embed_dims[3])

        # classification head
        # self.head = nn.Linear(embed_dims[3], num_classes) if num_classes > 0 else nn.Identity()

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            fan_out = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            fan_out //= m.groups
            m.weight.data.normal_(0, math.sqrt(2.0 / fan_out))
            if m.bias is not None:
                m.bias.data.zero_()

    def init_weights(self, pretrained=None):
        if isinstance(pretrained, str):
            logger = get_root_logger()
            load_checkpoint(self, pretrained, map_location='cpu', strict=False, logger=logger)

    def reset_drop_path(self, drop_path_rate):
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(self.depths))]
        cur = 0
        for i in range(self.depths[0]):
            self.block1[i].drop_path.drop_prob = dpr[cur + i]

        cur += self.depths[0]
        for i in range(self.depths[1]):
            self.block2[i].drop_path.drop_prob = dpr[cur + i]

        cur += self.depths[1]
        for i in range(self.depths[2]):
            self.block3[i].drop_path.drop_prob = dpr[cur + i]

        cur += self.depths[2]
        for i in range(self.depths[3]):
            self.block4[i].drop_path.drop_prob = dpr[cur + i]

    def freeze_patch_emb(self):
        self.patch_embed1.requires_grad = False

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed1', 'pos_embed2', 'pos_embed3', 'pos_embed4', 'cls_token'}  # has pos_embed may be better

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features1(self, x, cond):
        B = x.shape[0]

        # stage 1
        x, H, W = self.patch_embed1(x)
        cond, H, W = self.patch_embed1(cond)
        # print("stage 1 H shape is {}, W shape is {}".format(H, W))
        for i, blk in enumerate(self.block1):
            x = blk(x, H, W, cond)
            # print("stage 1 x shape is", x.shape)
        x = self.norm1(x)

        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        # print("stage 1 x shape is", x.shape)
        # outs.append(x)
        return x

    def forward_features2(self, x, cond):
        # stage 2
        B = x.shape[0]
        x, H, W = self.patch_embed2(x)
        cond, H, W = self.patch_embed2(cond)
        # print("stage 2 H shape is {}, W shape is {}".format(H, W))
        for i, blk in enumerate(self.block2):
            x = blk(x, H, W, cond)
            # print("stage 2 x shape is", x.shape)
        x = self.norm2(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        # print("stage 2 x shape is", x.shape)
        # outs.append(x)
        return x

    def forward_features3(self, x, cond):
        # stage 3
        B = x.shape[0]
        x, H, W = self.patch_embed3(x)
        cond, H, W = self.patch_embed3(cond)
        # print("stage 3 H shape is {}, W shape is {}".format(H, W))
        for i, blk in enumerate(self.block3):
            x = blk(x, H, W, cond)
            # print("stage 3 x shape is", x.shape)
        x = self.norm3(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        # print("stage 3 x shape is", x.shape)
        # outs.append(x)
        return x

    def forward_features4(self, x, cond):
        # stage 4
        B = x.shape[0]
        x, H, W = self.patch_embed4(x)
        cond, H, W = self.patch_embed4(cond)
        # print("stage 4 H shape is {}, W shape is {}".format(H, W))
        for i, blk in enumerate(self.block4):
            x = blk(x, H, W, cond)
            # print("stage 4 x shape is", x.shape)
        x = self.norm4(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        # print("stage 4 x shape is", x.shape)
        # outs.append(x)

        return x

    def forward(self, x):
        x, outs = self.forward_features1(x)
        x, outs = self.forward_features2(x)
        x, outs = self.forward_features3(x)
        x, outs = self.forward_features4(x)
        # x = self.head(x)

        return outs


class DWConv(nn.Module):
    def __init__(self, dim=768):
        super(DWConv, self).__init__()
        self.dwconv = nn.Conv2d(dim, dim, 3, 1, 1, bias=True, groups=dim)

    def forward(self, x, H, W):
        B, N, C = x.shape
        x = x.transpose(1, 2).view(B, C, H, W)
        x = self.dwconv(x)
        x = x.flatten(2).transpose(1, 2)

        return x



class mit_b0(MixVisionTransformer):
    def __init__(self, **kwargs):
        super(mit_b0, self).__init__(
            patch_size=4, embed_dims=[32, 64, 160, 256], num_heads=[1, 2, 5, 8], mlp_ratios=[4, 4, 4, 4],
            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[2, 2, 2, 2], sr_ratios=[8, 4, 2, 1],
            drop_rate=0.0, drop_path_rate=0.1)


class mit_b1(MixVisionTransformer):
    def __init__(self, **kwargs):
        super(mit_b1, self).__init__(
            patch_size=4, embed_dims=[64, 128, 320, 512], num_heads=[1, 2, 5, 8], mlp_ratios=[4, 4, 4, 4],
            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[2, 2, 2, 2], sr_ratios=[8, 4, 2, 1],
            drop_rate=0.0, drop_path_rate=0.1)


class mit_b2(MixVisionTransformer):
    def __init__(self, **kwargs):
        super(mit_b2, self).__init__(
            patch_size=4, embed_dims=[64, 128, 320, 512], num_heads=[1, 2, 5, 8], mlp_ratios=[4, 4, 4, 4],
            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 4, 6, 3], sr_ratios=[8, 4, 2, 1],
            drop_rate=0.0, drop_path_rate=0.1)


class mit_b3(MixVisionTransformer):
    def __init__(self, **kwargs):
        super(mit_b3, self).__init__(
            patch_size=4, embed_dims=[64, 128, 320, 512], num_heads=[1, 2, 5, 8], mlp_ratios=[4, 4, 4, 4],
            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 4, 18, 3], sr_ratios=[8, 4, 2, 1],
            drop_rate=0.0, drop_path_rate=0.1)


class mit_b4(MixVisionTransformer):
    def __init__(self, **kwargs):
        super(mit_b4, self).__init__(
            patch_size=4, embed_dims=[64, 128, 320, 512], num_heads=[1, 2, 5, 8], mlp_ratios=[4, 4, 4, 4],
            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 8, 27, 3], sr_ratios=[8, 4, 2, 1],
            drop_rate=0.0, drop_path_rate=0.1)


class mit_b5(MixVisionTransformer):
    def __init__(self, **kwargs):
        super(mit_b5, self).__init__(
            patch_size=4, embed_dims=[64, 128, 320, 512], num_heads=[1, 2, 5, 8], mlp_ratios=[4, 4, 4, 4],
            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[3, 6, 40, 3], sr_ratios=[8, 4, 2, 1],
            drop_rate=0.0, drop_path_rate=0.1)


class Cross_att(Block):
    def __init__(self, dim, num_heads):
        super(Cross_att, self).__init__()

        self.cross_att = self.attn_cond()

    def forward(self, x, H, W, cond):
        x = self.cross_att(self.norm1(x), H, W, self.norm1(cond))

        return x


class Segformer_implict(nn.Module):
    def __init__(self, backbone="mit_b5"):
        super(Segformer_implict, self).__init__()

        if backbone == "mit_b5":
            self.segformer = mit_b5()
            self.ckpt = torch.load(r"E:\pertrain_weight\segformer\mit_b5.pth")
            self.head = self.head = SegFormerHead(in_channels=[64, 128, 320, 512],
                                                    in_index=[0, 1, 2, 3],
                                                    feature_strides=[4, 8, 16, 32],
                                                    channels=128,
                                                    dropout_ratio=0.1,
                                                    num_classes=64,
                                                    align_corners=False,
                                                    decoder_params=dict({"embed_dim": 768}))
        elif backbone == "mit_b4":
            self.segformer = mit_b4()
            self.ckpt = torch.load(r"E:\pertrain_weight\segformer\mit_b4.pth")
            self.head = self.head = SegFormerHead(in_channels=[64, 128, 320, 512],
                                                  in_index=[0, 1, 2, 3],
                                                  feature_strides=[4, 8, 16, 32],
                                                  channels=128,
                                                  dropout_ratio=0.1,
                                                  num_classes=2,
                                                  align_corners=False,
                                                  decoder_params=dict({"embed_dim": 768}))
        elif backbone == "mit_b3":
            self.segformer = mit_b3()
            self.ckpt = torch.load(r"E:\pertrain_weight\segformer\mit_b3.pth")
            self.segformer.load_state_dict(self.ckpt, False)
            self.head = self.head = SegFormerHead(in_channels=[64, 128, 320, 512],
                                                  in_index=[0, 1, 2, 3],
                                                  feature_strides=[4, 8, 16, 32],
                                                  channels=128,
                                                  dropout_ratio=0.1,
                                                  num_classes=64,
                                                  align_corners=False,
                                                  decoder_params=dict({"embed_dim": 768}))
        elif backbone == "mit_b2":
            self.segformer = mit_b2()
            self.ckpt = torch.load(r"E:\pertrain_weight\segformer\mit_b2.pth")
            self.segformer.load_state_dict(self.ckpt, False)
            self.head = self.head = SegFormerHead(in_channels=[64, 128, 320, 512], #64 128 320 512
                                                  in_index=[0, 1, 2, 3],
                                                  feature_strides=[4, 8, 16, 32],
                                                  channels=128,
                                                  dropout_ratio=0.1,
                                                  num_classes=2,
                                                  align_corners=False,
                                                  decoder_params=dict({"embed_dim": 768}))
        elif backbone == "mit_b1":
            self.segformer = mit_b1()
            self.ckpt = torch.load(r"E:\pertrain_weight\segformer\mit_b1.pth")
            self.head = self.head = SegFormerHead(in_channels=[64, 128, 320, 512],
                                                  in_index=[0, 1, 2, 3],
                                                  feature_strides=[4, 8, 16, 32],
                                                  channels=128,
                                                  dropout_ratio=0.1,
                                                  num_classes=64,
                                                  align_corners=False,
                                                  decoder_params=dict({"embed_dim": 768}))
        elif backbone == "mit_b0":
            self.segformer = mit_b0()
            self.ckpt = torch.load(r"E:\pertrain_weight\segformer\mit_b0.pth")
            self.segformer.load_state_dict(self.ckpt, False)
            self.head = SegFormerHead(in_channels=[32, 64, 160, 256],
                                                  in_index=[0, 1, 2, 3],
                                                  feature_strides=[4, 8, 16, 32],
                                                  channels=128,
                                                  dropout_ratio=0.1,
                                                  num_classes=2, #64, 2
                                                  align_corners=False,
                                                  decoder_params=dict({"embed_dim": 768}))
        self.ckpt.pop("head.weight")
        self.ckpt.pop("head.bias")
        #     embed_dims=[32, 64, 160, 256], num_heads=[1, 2, 5, 8], mlp_ratios=[4, 4, 4, 4],
        #             qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), depths=[2, 2, 2, 2], sr_ratios=[8, 4, 2, 1],
        #             drop_rate=0.0, drop_path_rate=0.1
        #     embed_dims = [32, 64, 160, 256]
        #     num_heads = [1, 2, 5, 8]
        #     sr_ratios = [8, 4, 2, 1]
        # self.upsample4x = nn.Upsample(scale_factor=4, mode='bilinear')
        self.conv1 = nn.Conv2d(in_channels=64, out_channels=64, kernel_size=7, padding=7//2, stride=1, bias=False)
        # self.conv1 = DeformConv2d(inc=64, outc=64, kernel_size=7, padding=7//2, stride=1, bias=False)
        self.bn = nn.BatchNorm2d(64)
        self.relu = nn.ReLU()
        self.bn1 = nn.BatchNorm2d(32)
        self.relu1 = nn.ReLU()
        self.conv2 = nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, padding=3 //2, stride=1, bias=False)
        # self.conv2 = DeformConv2d(inc=64, outc=32, kernel_size=3, padding=3 // 2, stride=1, bias=False)
        self.conv3 = nn.Conv2d(in_channels=32, out_channels=2, kernel_size=7, padding=7 // 2, stride=1, bias=False)
        self.conv4 = nn.Conv2d(in_channels=64, out_channels=2, kernel_size=7, padding=7 // 2, stride=1, bias=False)
        # self.conv3 = DeformConv2d(inc=32, outc=2, kernel_size=7, padding=7 // 2, stride=1, bias=False)
        self.diff_conv = nn.Conv2d(in_channels=3, out_channels=32, kernel_size=3, padding=3//2, stride=1, bias=False)
        self.mscam = MS_CAM(channels=32, r=2)
        self.seg_smallconv = nn.Conv2d(in_channels=64, out_channels=32, kernel_size=7, padding=7//2, stride=1, bias=False)
        self.seg_smallconv1 = nn.Conv2d(in_channels=32, out_channels=2, kernel_size=7, padding=7 // 2, stride=1,
                                       bias=False)
        self.seg_small_bn = nn.BatchNorm2d(32)
        self.seg_small_relu = nn.ReLU()
        self.diff_softmax1 = nn.Softmax(dim=-1)
        self.diff_softmax2 = nn.Softmax(dim=-1)
        self.diff_sigmoid_1 = nn.Sigmoid()
        self.diff_sigmoid_2 = nn.Sigmoid()
        # seg_b0
        self.diffflow1 = DiffFlowN(inplane=32, h=64, w=64)
        self.diffflow2 = DiffFlowN(inplane=64, h=32, w=32)
        self.diffflow3 = DiffFlowN(inplane=160, h=16, w=16)
        self.diffflow4 = DiffFlowN(inplane=256, h=8, w=8)
        #seg_b2 64, 128, 320, 512
        # self.diffflow1 = DiffFlowN(inplane=64, h=64, w=64)
        # self.diffflow2 = DiffFlowN(inplane=128, h=32, w=32)
        # self.diffflow3 = DiffFlowN(inplane=320, h=16, w=16)
        # self.diffflow4 = DiffFlowN(inplane=512, h=8, w=8)
        self.ifa = fpn_ifa(in_planes=256, ultra_pe=True, pos_dim=24, no_aspp=True, require_grad=True)
    #     inner_planes: 256
    #       dilations: [12, 24, 36]
    #       ultra_pe: True
    #       pos_dim: 24
    #       no_aspp: True
    #       require_grad: True


    def forward(self, x1, x2):

        diff_list = []

        #stage 1
        x1_1 = self.segformer.forward_features1(x1, x2)
        x2_1 = self.segformer.forward_features1(x2, x1) #[8, 32, 64, 64]

        diff0 = self.diffflow1(x1_1, x2_1)

        # diff0 = torch.abs(x1_1 - x2_1)

        #stage 2
        x1_2 = self.segformer.forward_features2(x1_1, x2_1)
        x2_2 = self.segformer.forward_features2(x2_1, x1_1)

        diff1 = self.diffflow2(x1_2, x2_2)
        # diff1 = torch.abs(x1_2 - x2_2)
        # print(diff1.shape)

        #stage 3
        x1_3 = self.segformer.forward_features3(x1_2, x2_2)
        x2_3 = self.segformer.forward_features3(x2_2, x1_2)

        # diff2 = torch.abs(x1_3 - x2_3)
        diff2 = self.diffflow3(x1_3, x2_3)
        # print(diff2.shape)

        #stage 4
        x1_4 = self.segformer.forward_features4(x1_3, x2_3)
        x2_4 = self.segformer.forward_features4(x2_3, x1_3)
        diff3 = torch.abs(x1_4 - x2_4)
        # diff3 = self.diffflow4(x1_4, x2_4)
        # print(diff3.shape)

        diff_list.append(diff0)
        diff_list.append(diff1)
        diff_list.append(diff2)
        diff_list.append(diff3)
        # print(diff_list[0].shape)
        # print(diff_list[1].shape)
        # print(diff_list[2].shape)
        # print(diff_list[3].shape)

        segmap_orign = self.ifa(diff_list)
        # segmap_small = self.head(diff_list) #[batch, 64, 64, 64] ->2 64 64
        # segmap_orign = F.interpolate(segmap_small, size=(256, 256), mode='bilinear', align_corners=False)
        # segmap_orign = F.interpolate(segmap_small, size=(512, 512), mode='bilinear', align_corners=False)
        # segmap_orign = self.conv1(segmap_small)
        # segmap_orign = self.bn(segmap_orign)
        # segmap_orign = self.relu(segmap_orign)
        # segmap_orign = self.conv2(segmap_orign)
        # segmap_orign = self.bn1(segmap_orign)
        # segmap_orign = self.relu1(segmap_orign)
        # segmap_orign = F.interpolate(segmap_orign, size=(256, 256), mode='bilinear', align_corners=False)
        # segmap_orign = self.conv3(segmap_orign)
        return segmap_orign






if __name__ == "__main__":
    # torch.Size([8, 32, 64, 64])
    # torch.Size([8, 64, 32, 32])
    # torch.Size([8, 160, 16, 16])
    # torch.Size([8, 256, 8, 8])
    img = torch.randn([8, 3, 256, 256])
    gt = torch.randn([8, 256, 256])
    seg = Segformer_implict(backbone="mit_b0")

    res1  = seg(img, img)
    print("res shape is", res1.shape)


