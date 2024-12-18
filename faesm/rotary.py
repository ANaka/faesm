# Adapted from flash_attn trition based rotary to support variable sequence length
# https://github.com/Dao-AILab/flash-attention/blob/main/flash_attn/layers/rotary.py
from typing import Tuple, Union

import torch
from flash_attn.ops.triton.rotary import apply_rotary


class ApplyRotaryEmbQKV_(torch.autograd.Function):
    @staticmethod
    def forward(ctx, qkv, cos, sin, cu_seqlens, max_seqlen):
        q, k = qkv[:, 0], qkv[:, 1]

        apply_rotary(q, cos, sin, cu_seqlens=cu_seqlens, max_seqlen=max_seqlen, inplace=True)
        apply_rotary(k, cos, sin, cu_seqlens=cu_seqlens, max_seqlen=max_seqlen, inplace=True)

        ctx.save_for_backward(cos, sin, cu_seqlens)
        ctx.max_seqlen = max_seqlen

        return qkv

    @staticmethod
    def backward(ctx, dqkv):
        max_seqlen = ctx.max_seqlen
        cos, sin, cu_seqlens = ctx.saved_tensors

        dq, dk = dqkv[:, 0], dqkv[:, 1]

        apply_rotary(
            dq,
            cos,
            sin,
            cu_seqlens=cu_seqlens,
            max_seqlen=max_seqlen,
            inplace=True,
            conjugate=True,
        )
        apply_rotary(
            dk,
            cos,
            sin,
            cu_seqlens=cu_seqlens,
            max_seqlen=max_seqlen,
            inplace=True,
            conjugate=True,
        )

        return dqkv, None, None, None, None


def apply_rotary_emb_qkv_(
    qkv, cos, sin, cu_seqlens: torch.Tensor, max_seqlen: int
) -> torch.Tensor:
    """Apply rotary embedding *inplace* to the first rotary_dim of Q and K.

    Arguments:
        qkv: (batch_size * seqlen, 3, nheads, headdim)
        cos, sin: (seqlen, rotary_dim / 2)
        cu_seqlen: (batch_size + 1) the cumulative sum of the sequence lengths
        max_seqlen: int the maximum sequence length in the batch
    Return:
        qkv: (batch_size * seqlen, 3, nheads, headdim)
    """
    return ApplyRotaryEmbQKV_.apply(qkv, cos, sin, cu_seqlens, max_seqlen)


class RotaryEmbedding(torch.nn.Module):
    """The rotary position embeddings from RoFormer_ (Su et.

    al).
    """

    def __init__(self, dim: int, base=10000.0, pos_idx_in_fp32=True, device=None):
        """
        pos_idx_in_fp32: if True, the position indices [0.0, ..., seqlen - 1] are in fp32,
            otherwise they might be in lower precision.
            This option was added because previously (before 2023-07-02), when we construct
            the position indices, we use the dtype of self.inv_freq. In most cases this would
            be fp32, but if the model is trained in pure bf16 (not mixed precision), then
            self.inv_freq would be bf16, and the position indices are also in bf16.
            Because of the limited precision of bf16 (e.g. 1995.0 is rounded to 2000.0), the
            embeddings for some positions will coincide.
            To maintain compatibility with models previously trained in pure bf16,
            we add this option.
        """
        super().__init__()
        self.dim = dim
        self.base = float(base)
        self.pos_idx_in_fp32 = pos_idx_in_fp32
        inv_freq = self._compute_inv_freq(device)
        self.register_buffer("inv_freq", inv_freq)
        self._seq_len_cached = 0
        self._cos_cached = None
        self._sin_cached = None

    def _compute_inv_freq(self, device=None):
        return 1.0 / (
            self.base
            ** (torch.arange(0, self.dim, 2, device=device, dtype=torch.float32) / self.dim)
        )

    def _update_cos_sin_cache(self, seqlen, device=None, dtype=None):
        # Reset the tables if the sequence length has changed,
        # if we're on a new device (possibly due to tracing for instance),
        # or if we're switching from inference mode to training
        if (
            seqlen > self._seq_len_cached
            or self._cos_cached is None
            or self._cos_cached.device != device
            or self._cos_cached.dtype != dtype
            or (self.training and self._cos_cached.is_inference())
        ):
            self._seq_len_cached = seqlen
            # We want fp32 here, not self.inv_freq.dtype, since the model could be loaded in bf16
            # And the output of arange can be quite large, so bf16 would lose a lot of precision.
            # However, for compatibility reason, we add an option to use the dtype of self.inv_freq.
            if self.pos_idx_in_fp32:
                t = torch.arange(seqlen, device=device, dtype=torch.float32)
                # We want fp32 here as well since inv_freq will be multiplied with t, and the output
                # will be large. Having it in bf16 will lose a lot of precision and cause the
                # cos & sin output to change significantly.
                # We want to recompute self.inv_freq if it was not loaded in fp32
                if self.inv_freq.dtype != torch.float32:
                    inv_freq = self._compute_inv_freq(device=device)
                else:
                    inv_freq = self.inv_freq
            else:
                t = torch.arange(seqlen, device=device, dtype=self.inv_freq.dtype)
                inv_freq = self.inv_freq

            freqs = torch.outer(t, inv_freq)
            self._cos_cached = torch.cos(freqs).to(dtype)
            self._sin_cached = torch.sin(freqs).to(dtype)

    def forward(
        self, qkv: torch.Tensor, cu_seqlens: torch.Tensor, max_seqlen: int, *args, **kwargs
    ) -> torch.Tensor:
        """Apply rotary embedding *inplace*.

        Arguments:
            qkv: (batch * seqlen, 3, nheads, headdim) query, key, value.
            cu_seqlens: (batch + 1) the cumulative sum of the sequence lengths.
            max_seqlen: int the maximum sequence length in the batch.
        Return:
            qkv: (batch_size * seqlen, 3, nheads, headdim)
        """
        self._update_cos_sin_cache(max_seqlen, device=qkv.device, dtype=qkv.dtype)

        return apply_rotary_emb_qkv_(
            qkv,
            self._cos_cached,
            self._sin_cached,
            cu_seqlens=cu_seqlens,
            max_seqlen=max_seqlen,
        )
