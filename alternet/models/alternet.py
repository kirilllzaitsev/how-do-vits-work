import sys; sys.path.append('/mnt/wext/msc_studies/monodepth_project/related_work/how-do-vits-work')
from functools import partial
from itertools import cycle

import alternet.models.classifier_block as classifier
import alternet.models.layers as layers
import alternet.models.preresnet_dnn_block as preresnet_dnn
import alternet.models.preresnet_mcdo_block as preresnet_mcdo
import alternet.models.resnet_dnn_block as resnet_dnn
import alternet.models.resnet_mcdo_block as resnet_mcdo
import numpy as np
import torch
import torch.nn as nn
from einops import rearrange
from alternet.models.attentions import Attention2d
from alternet.models.layers import DropPath, conv1x1


class LocalAttention(nn.Module):
    def __init__(
        self,
        dim_in,
        dim_out=None,
        *,
        window_size=7,
        k=1,
        heads=8,
        dim_head=32,
        dropout=0.0
    ):
        super().__init__()
        self.attn = Attention2d(
            dim_in, dim_out, heads=heads, dim_head=dim_head, dropout=dropout, k=k
        )
        self.window_size = window_size

        self.rel_index = self.rel_distance(window_size) + window_size - 1
        self.pos_embedding = nn.Parameter(
            torch.randn(2 * window_size - 1, 2 * window_size - 1) * 0.02
        )

    def forward(self, x, mask=None):
        b, c, h, w = x.shape
        p = self.window_size
        n1 = h // p
        n2 = w // p

        mask = torch.zeros(p**2, p**2, device=x.device) if mask is None else mask
        mask = (
            mask + self.pos_embedding[self.rel_index[:, :, 0], self.rel_index[:, :, 1]]
        )

        x = rearrange(x, "b c (n1 p1) (n2 p2) -> (b n1 n2) c p1 p2", p1=p, p2=p)
        x, attn = self.attn(x, mask)
        x = rearrange(
            x, "(b n1 n2) c p1 p2 -> b c (n1 p1) (n2 p2)", n1=n1, n2=n2, p1=p, p2=p
        )

        return x, attn

    @staticmethod
    def rel_distance(window_size):
        i = torch.tensor(
            np.array([[x, y] for x in range(window_size) for y in range(window_size)])
        )
        d = i[None, :, :] - i[:, None, :]

        return d


# Attention Blocks


class AttentionBlockA(nn.Module):
    # Attention block with post-activation.
    # This block is for ablation study, and we do NOT use this block by default.
    expansion = 4

    def __init__(
        self,
        dim_in,
        dim_out=None,
        *,
        heads=8,
        dim_head=64,
        dropout=0.0,
        sd=0.0,
        stride=1,
        window_size=7,
        k=1,
        norm=nn.BatchNorm2d,
        activation=nn.GELU,
        **block_kwargs
    ):
        super().__init__()
        dim_out = dim_in if dim_out is None else dim_out
        attn = partial(LocalAttention, window_size=window_size, k=k)
        width = dim_in // self.expansion

        self.shortcut = []
        if dim_in != dim_out * self.expansion:
            self.shortcut.append(conv1x1(dim_in, dim_out * self.expansion))
            self.shortcut.append(norm(dim_out * self.expansion))
        self.shortcut = nn.Sequential(*self.shortcut)

        self.conv = nn.Sequential(
            conv1x1(dim_in, width, stride=stride),
            norm(width),
            activation(),
        )
        self.attn = attn(
            width,
            dim_out * self.expansion,
            heads=heads,
            dim_head=dim_head,
            dropout=dropout,
        )
        self.norm = norm(dim_out * self.expansion)
        self.sd = DropPath(sd) if sd > 0.0 else nn.Identity()

    def forward(self, x):
        skip = self.shortcut(x)
        x = self.conv(x)
        x, attn = self.attn(x)
        x = self.norm(x)
        x = self.sd(x) + skip

        return x


class AttentionBasicBlockA(AttentionBlockA):
    expansion = 1


class AttentionBlockB(nn.Module):
    # Attention block with pre-activation.
    # We use this block by default.
    expansion = 4

    def __init__(
        self,
        dim_in,
        dim_out=None,
        *,
        heads=8,
        dim_head=64,
        dropout=0.0,
        sd=0.0,
        stride=1,
        window_size=7,
        k=1,
        norm=nn.BatchNorm2d,
        activation=nn.GELU,
        return_attn=False,
        **block_kwargs
    ):
        super().__init__()
        self.return_attn = return_attn
        dim_out = dim_in if dim_out is None else dim_out
        attn = partial(LocalAttention, window_size=window_size, k=k)
        width = dim_in // self.expansion

        self.shortcut = []
        if stride != 1 or dim_in != dim_out * self.expansion:
            self.shortcut.append(
                layers.conv1x1(dim_in, dim_out * self.expansion, stride=stride)
            )
        self.shortcut = nn.Sequential(*self.shortcut)
        self.norm1 = norm(dim_in)
        self.relu = activation()

        self.conv = nn.Conv2d(dim_in, width, kernel_size=1, bias=False)
        self.norm2 = norm(width)
        self.attn = attn(
            width,
            dim_out * self.expansion,
            heads=heads,
            dim_head=dim_head,
            dropout=dropout,
        )
        self.sd = DropPath(sd) if sd > 0.0 else nn.Identity()

    def forward(self, x):
        if len(self.shortcut) > 0:
            x = self.norm1(x)
            x = self.relu(x)
            skip = self.shortcut(x)
        else:
            skip = self.shortcut(x)
            x = self.norm1(x)
            x = self.relu(x)

        x = self.conv(x)
        x = self.norm2(x)

        x, attn = self.attn(x)

        x = self.sd(x) + skip

        if self.return_attn:
            return x, attn
        return x


class AttentionBasicBlockB(AttentionBlockB):
    expansion = 1


# Stems


class StemA(nn.Module):
    # Typical Stem stage for CNNs, e.g. ResNet or ResNeXt.
    # This block is for ablation study, and we do NOT use this block by default.

    def __init__(self, dim_in, dim_out, pool=True):
        super().__init__()

        self.layer0 = []
        if pool:
            self.layer0.append(
                layers.convnxn(dim_in, dim_out, kernel_size=7, stride=2, padding=3)
            )
            self.layer0.append(layers.bn(dim_out))
            self.layer0.append(layers.relu())
            self.layer0.append(nn.MaxPool2d(kernel_size=3, stride=2, padding=1))
        else:
            self.layer0.append(layers.conv3x3(dim_in, dim_out, stride=1))
            self.layer0.append(layers.bn(dim_out))
            self.layer0.append(layers.relu())
        self.layer0 = nn.Sequential(*self.layer0)

    def forward(self, x):
        x = self.layer0(x)
        return x


class StemB(nn.Module):
    # Stem stage for pre-activation pattern based on pre-activation ResNet.
    # We use this block by default.

    def __init__(self, dim_in, dim_out, pool=True):
        super().__init__()

        self.layer0 = []
        if pool:
            self.layer0.append(
                layers.convnxn(dim_in, dim_out, kernel_size=7, stride=2, padding=3)
            )
            self.layer0.append(nn.MaxPool2d(kernel_size=3, stride=2, padding=1))
        else:
            self.layer0.append(layers.conv3x3(dim_in, dim_out, stride=1))
        self.layer0 = nn.Sequential(*self.layer0)

    def forward(self, x):
        x = self.layer0(x)
        return x


# Model


class AlterNet(nn.Module):
    def __init__(
        self,
        block1,
        block2,
        *,
        num_blocks,
        num_blocks2,
        heads,
        cblock=classifier.BNGAPBlock,
        sd=0.0,
        num_classes=10,
        stem=StemB,
        name="alternet",
        **block_kwargs
    ):
        super().__init__()
        self.name = name
        idxs = [
            [j for j in range(sum(num_blocks[:i]), sum(num_blocks[: i + 1]))]
            for i in range(len(num_blocks))
        ]
        sds = [[sd * j / (sum(num_blocks) - 1) for j in js] for js in idxs]

        self.layer0 = stem(3, 64)
        self.layer1 = self._make_layer(
            block1,
            block2,
            64,
            64,
            num_blocks[0],
            num_blocks2[0],
            stride=1,
            heads=heads[0],
            sds=sds[0],
            **block_kwargs
        )
        self.layer2 = self._make_layer(
            block1,
            block2,
            64 * block2.expansion,
            128,
            num_blocks[1],
            num_blocks2[1],
            stride=2,
            heads=heads[1],
            sds=sds[1],
            **block_kwargs
        )
        self.layer3 = self._make_layer(
            block1,
            block2,
            128 * block2.expansion,
            256,
            num_blocks[2],
            num_blocks2[2],
            stride=2,
            heads=heads[2],
            sds=sds[2],
            **block_kwargs
        )
        self.layer4 = self._make_layer(
            block1,
            block2,
            256 * block2.expansion,
            512,
            num_blocks[3],
            num_blocks2[3],
            stride=2,
            heads=heads[3],
            sds=sds[3],
            **block_kwargs
        )

        self.classifier = []
        if cblock is classifier.MLPBlock:
            self.classifier.append(nn.AdaptiveAvgPool2d((7, 7)))
            self.classifier.append(
                cblock(7 * 7 * 512 * block2.expansion, num_classes, **block_kwargs)
            )
        else:
            self.classifier.append(
                cblock(512 * block2.expansion, num_classes, **block_kwargs)
            )
        self.classifier = nn.Sequential(*self.classifier)

    @staticmethod
    def _make_layer(
        block1,
        block2,
        in_channels,
        out_channels,
        num_block1,
        num_block2,
        stride,
        heads,
        sds,
        **block_kwargs
    ):
        alt_seq = [False] * (num_block1 - num_block2 * 2) + [False, True] * num_block2
        stride_seq = [stride] + [1] * (num_block1 - 1)

        seq, channels = [], in_channels
        for alt, stride, sd in zip(alt_seq, stride_seq, sds):
            block = block1 if not alt else block2
            seq.append(
                block(
                    channels,
                    out_channels,
                    stride=stride,
                    sd=sd,
                    heads=heads,
                    **block_kwargs
                )
            )
            channels = out_channels * block.expansion

        return nn.Sequential(*seq)

    def forward(self, x):
        x = self.layer0(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.classifier(x)

        return x


def dnn_18(num_classes=1000, stem=True, name="alternet_18", **block_kwargs):
    return AlterNet(
        preresnet_dnn.BasicBlock,
        AttentionBasicBlockB,
        stem=partial(StemB, pool=stem),
        num_blocks=(2, 2, 2, 2),
        num_blocks2=(0, 1, 1, 1),
        heads=(3, 6, 12, 24),
        num_classes=num_classes,
        name=name,
        **block_kwargs
    )


def plot_attention(att_mat, img_size):
    # att_mat = torch.stack(att_mat).squeeze(1)

    # Average the attention weights across all heads.
    att_mat = torch.mean(att_mat, dim=1)

    # To account for residual connections, we add an identity matrix to the
    # attention matrix and re-normalize the weights.
    residual_att = torch.eye(att_mat.size(1))
    aug_att_mat = att_mat + residual_att
    aug_att_mat = aug_att_mat / aug_att_mat.sum(dim=-1).unsqueeze(-1)

    # Recursively multiply the weight matrices
    joint_attentions = torch.zeros(aug_att_mat.size())
    joint_attentions[0] = aug_att_mat[0]

    for n in range(1, aug_att_mat.size(0)):
        joint_attentions[n] = torch.matmul(aug_att_mat[n], joint_attentions[n - 1])

    # Attention from the output token to the input space.
    v = joint_attentions[-1]
    grid_size = int(np.sqrt(aug_att_mat.size(-1)))
    mask = v[0, 0:].reshape(grid_size, grid_size).detach().numpy()
    # mask = v[0, 1:].reshape(grid_size, grid_size).detach().numpy()
    import cv2

    print(mask.shape)
    mask = cv2.resize(mask / mask.max(), img_size)[..., np.newaxis]
    return mask


if __name__ == "__main__":
    model = dnn_18(num_classes=10)
    # y = model(x)
    # print(y.shape)
    block = """
AttentionBasicBlockB(
      (shortcut): Sequential()
      (norm1): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)
      (relu): GELU(approximate='none')
      (conv): Conv2d(128, 128, kernel_size=(1, 1), stride=(1, 1), bias=False)
      (norm2): BatchNorm2d(128, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True)
      (attn): LocalAttention(
        (attn): Attention2d(
          (to_q): Conv2d(128, 384, kernel_size=(1, 1), stride=(1, 1), bias=False)
          (to_kv): Conv2d(128, 768, kernel_size=(1, 1), stride=(1, 1), bias=False)
          (to_out): Sequential(
            (0): Conv2d(384, 128, kernel_size=(1, 1), stride=(1, 1))
            (1): Identity()
          )
        )
      )
      (sd): Identity()
    )
"""
    x_dim = 960
    x_size = 8
    x = torch.randn(1, x_dim, x_size, x_size)
    block = AttentionBasicBlockB(x_dim, x_dim, stride=1, heads=4, window_size=4)
    yblock = block(x)
    print(yblock.shape)
