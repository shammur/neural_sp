#! /usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2018 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""RNN decoder for Listen Attend and Spell (LAS) model (including CTC loss calculation)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import editdistance
import logging
import math
import numpy as np
import os
import random
import shutil
import torch
import torch.nn as nn

from neural_sp.models.criterion import cross_entropy_lsm
from neural_sp.models.criterion import distillation
from neural_sp.models.lm.rnnlm import RNNLM
from neural_sp.models.modules.gmm_attention import GMMAttention
from neural_sp.models.modules.mocha import MoChA
from neural_sp.models.modules.multihead_attention import MultiheadAttentionMechanism
from neural_sp.models.modules.singlehead_attention import AttentionMechanism
from neural_sp.models.seq2seq.decoders.beam_search import BeamSearch
from neural_sp.models.seq2seq.decoders.ctc import CTC
from neural_sp.models.seq2seq.decoders.ctc import CTCPrefixScore
from neural_sp.models.seq2seq.decoders.decoder_base import DecoderBase
from neural_sp.models.seq2seq.decoders.mbr import MBR
from neural_sp.models.torch_utils import append_sos_eos
from neural_sp.models.torch_utils import compute_accuracy
from neural_sp.models.torch_utils import make_pad_mask
from neural_sp.models.torch_utils import pad_list
from neural_sp.models.torch_utils import repeat
from neural_sp.models.torch_utils import np2tensor
from neural_sp.models.torch_utils import tensor2np
from neural_sp.utils import mkdir_join

import matplotlib
matplotlib.use('Agg')

random.seed(1)

logger = logging.getLogger(__name__)


class RNNDecoder(DecoderBase):
    """RNN decoder.

    Args:
        special_symbols (dict):
            eos (int): index for <eos> (shared with <sos>)
            unk (int): index for <unk>
            pad (int): index for <pad>
            blank (int): index for <blank>
        enc_n_units (int): number of units of the encoder outputs
        attn_type (str): type of attention mechanism
        rnn_type (str): lstm/gru
        n_units (int): number of units in each RNN layer
        n_projs (int): number of units in each projection layer
        n_layers (int): number of RNN layers
        bottleneck_dim (int): dimension of the bottleneck layer before the softmax layer for label generation
        emb_dim (int): dimension of the embedding in target spaces.
        vocab (int): number of nodes in softmax layer
        tie_embedding (bool): tie parameters of the embedding and output layers
        attn_dim (int):
        attn_sharpening_factor (float):
        attn_sigmoid_smoothing (bool):
        attn_conv_out_channels (int):
        attn_conv_kernel_size (int):
        attn_n_heads (int): number of attention heads
        dropout (float): dropout probability for the RNN layer
        dropout_emb (float): dropout probability for the embedding layer
        dropout_att (float): dropout probability for attention distributions
        lsm_prob (float): label smoothing probability
        ss_prob (float): scheduled sampling probability
        ss_type (str): constant/saturation
        ctc_weight (float): CTC loss weight
        ctc_lsm_prob (float): label smoothing probability for CTC
        ctc_fc_list (list):
        mbr_weight (float): MBR loss weight
        mbr_nbest (int): N-best for MBR training
        mbr_softmax_smoothing (int): softmax smoothing (beta) for MBR training
        lm_fusion (RNNLM):
        lm_fusion_type (str): type of LM fusion
        lm_init (RNNLM):
        backward (bool): decode in the backward order
        global_weight (float):
        mtl_per_batch (bool):
        param_init (float):
        mocha_chunk_size (int): chunk size for MoChA
        mocha_adaptive (bool): adaptive MoChA
        mocha_1dconv (bool): 1dconv for MoChA
        mocha_quantity_loss_weight (float):
        mocha_ctc_sync (str):
        gmm_attn_n_mixtures (int):
        replace_sos (bool):
        soft_label_weight (float):
        discourse_aware (str): state_carry_over/hierarchical

    """

    def __init__(self, special_symbols,
                 enc_n_units, attn_type, rnn_type, n_units, n_projs, n_layers,
                 bottleneck_dim, emb_dim, vocab, tie_embedding,
                 attn_dim, attn_sharpening_factor, attn_sigmoid_smoothing,
                 attn_conv_out_channels, attn_conv_kernel_size, attn_n_heads,
                 dropout, dropout_emb, dropout_att,
                 lsm_prob, ss_prob, ss_type,
                 ctc_weight, ctc_lsm_prob, ctc_fc_list,
                 mbr_weight, mbr_nbest, mbr_softmax_smoothing,
                 lm_fusion, lm_fusion_type, lm_init,
                 backward, global_weight, mtl_per_batch,
                 param_init,
                 mocha_chunk_size=1,
                 mocha_adaptive=False,
                 mocha_1dconv=False,
                 mocha_quantity_loss_weight=0.,
                 mocha_ctc_sync=False,
                 gmm_attn_n_mixtures=5,
                 replace_sos=False,
                 soft_label_weight=0.,
                 discourse_aware=''):

        super(RNNDecoder, self).__init__()

        self.eos = special_symbols['eos']
        self.unk = special_symbols['unk']
        self.pad = special_symbols['pad']
        self.blank = special_symbols['blank']
        self.vocab = vocab
        self.attn_type = attn_type
        self.rnn_type = rnn_type
        assert rnn_type in ['lstm', 'gru']
        self.enc_n_units = enc_n_units
        self.dec_n_units = n_units
        self.n_projs = n_projs
        self.n_layers = n_layers
        self.lsm_prob = lsm_prob
        self.ss_prob = ss_prob
        self.ss_type = ss_type
        if ss_type == 'constant':
            self._ss_prob = ss_prob
        elif ss_type == 'saturation':
            self._ss_prob = 0  # start from 0
        self.att_weight = global_weight - ctc_weight
        self.ctc_weight = ctc_weight
        self.lm_fusion_type = lm_fusion_type
        self.bwd = backward
        self.mtl_per_batch = mtl_per_batch
        self.replace_sos = replace_sos
        self.soft_label_weight = soft_label_weight

        # for mocha
        self.quantity_loss_weight = mocha_quantity_loss_weight
        self.mocha_ctc_sync = mocha_ctc_sync

        # for MBR training
        self.mbr_weight = mbr_weight
        self.mbr_nbest = mbr_nbest
        self.mbr_softmax_smoothing = mbr_softmax_smoothing
        self.loss_scale = 0.01 if mbr_weight > 0 else 1.0
        if mbr_weight > 0:
            self.mbr = MBR.apply

        # for contextualization
        self.discourse_aware = discourse_aware
        self.dstate_prev = None

        self.prev_spk = ''
        self.dstates_final = None
        self.lmstate_final = None

        if ctc_weight > 0:
            self.ctc = CTC(eos=self.eos,
                           blank=self.blank,
                           enc_n_units=enc_n_units,
                           vocab=vocab,
                           dropout=dropout,
                           lsm_prob=ctc_lsm_prob,
                           fc_list=ctc_fc_list,
                           param_init=param_init)

        if self.att_weight > 0:
            # Attention layer
            qdim = n_units if n_projs == 0 else n_projs
            if attn_type == 'mocha':
                assert attn_n_heads == 1
                self.score = MoChA(enc_n_units, qdim, attn_dim,
                                   chunk_size=mocha_chunk_size,
                                   adaptive=mocha_adaptive,
                                   conv1d=mocha_1dconv,
                                   sharpening_factor=attn_sharpening_factor)
            elif attn_type == 'gmm':
                self.score = GMMAttention(enc_n_units, qdim, attn_dim,
                                          n_mixtures=gmm_attn_n_mixtures)
            else:
                if attn_n_heads > 1:
                    self.score = MultiheadAttentionMechanism(
                        enc_n_units, qdim, attn_dim, attn_type,
                        n_heads=attn_n_heads,
                        dropout=dropout_att)
                else:
                    self.score = AttentionMechanism(
                        enc_n_units, qdim, attn_dim, attn_type,
                        sharpening_factor=attn_sharpening_factor,
                        sigmoid_smoothing=attn_sigmoid_smoothing,
                        conv_out_channels=attn_conv_out_channels,
                        conv_kernel_size=attn_conv_kernel_size,
                        dropout=dropout_att)

            # Decoder
            self.rnn = nn.ModuleList()
            cell = nn.LSTMCell if rnn_type == 'lstm' else nn.GRUCell
            if self.n_projs > 0:
                self.proj = repeat(nn.Linear(n_units, n_projs), n_layers)
            self.dropout = nn.Dropout(p=dropout)
            dec_odim = enc_n_units + emb_dim
            for l in range(n_layers):
                self.rnn += [cell(dec_odim, n_units)]
                dec_odim = n_units
                if self.n_projs > 0:
                    dec_odim = n_projs

            # LM fusion
            if lm_fusion is not None:
                self.linear_dec_feat = nn.Linear(dec_odim + enc_n_units, n_units)
                if lm_fusion_type in ['cold', 'deep']:
                    self.linear_lm_feat = nn.Linear(lm_fusion.n_units, n_units)
                    self.linear_lm_gate = nn.Linear(n_units * 2, n_units)
                elif lm_fusion_type == 'cold_prob':
                    self.linear_lm_feat = nn.Linear(lm_fusion.vocab, n_units)
                    self.linear_lm_gate = nn.Linear(n_units * 2, n_units)
                else:
                    raise ValueError(lm_fusion_type)
                self.output_bn = nn.Linear(n_units * 2, bottleneck_dim)

                # fix LM parameters
                for p in lm_fusion.parameters():
                    p.requires_grad = False
            else:
                self.output_bn = nn.Linear(dec_odim + enc_n_units, bottleneck_dim)

            self.embed = nn.Embedding(vocab, emb_dim, padding_idx=self.pad)
            self.dropout_emb = nn.Dropout(p=dropout_emb)
            self.output = nn.Linear(bottleneck_dim, vocab)
            if tie_embedding:
                if emb_dim != bottleneck_dim:
                    raise ValueError('When using the tied flag, n_units must be equal to emb_dim.')
                self.output.weight = self.embed.weight

        self.reset_parameters(param_init)

        # resister the external LM
        self.lm = lm_fusion

        # decoder initialization with pre-trained LM
        if lm_init is not None:
            assert lm_init.vocab == vocab
            assert lm_init.n_units == n_units
            assert lm_init.emb_dim == emb_dim
            logger.info('===== Initialize the decoder with pre-trained RNNLM')
            assert lm_init.n_projs == 0  # TODO(hirofumi): fix later
            assert lm_init.n_units_null_context == enc_n_units

            # RNN
            for l in range(lm_init.n_layers):
                for n, p in lm_init.rnn[l].named_parameters():
                    assert getattr(self.rnn[l], n).size() == p.size()
                    getattr(self.rnn[l], n).data = p.data
                    logger.info('Overwrite %s' % n)

            # embedding
            assert self.embed.weight.size() == lm_init.embed.weight.size()
            self.embed.weight.data = lm_init.embed.weight.data
            logger.info('Overwrite %s' % 'embed.weight')

    def reset_parameters(self, param_init):
        """Initialize parameters with uniform distribution."""
        logger.info('===== Initialize %s with uniform distribution =====' % self.__class__.__name__)
        for n, p in self.named_parameters():
            if 'score.monotonic_energy.v.weight_g' in n or 'score.monotonic_energy.r' in n:
                logger.info('Skip initialization of %s' % n)
                continue
            if 'score.chunk_energy.v.weight_g' in n or 'score.chunk_energy.r' in n:
                logger.info('Skip initialization of %s' % n)
                continue

            if p.dim() == 1:
                if 'linear_lm_gate.fc.bias' in n:
                    # Initialize bias in gating with -1 for cold fusion
                    nn.init.constant_(p, -1.)  # bias
                    logger.info('Initialize %s with %s / %.3f' % (n, 'constant', -1.))
                else:
                    nn.init.constant_(p, 0.)  # bias
                    logger.info('Initialize %s with %s / %.3f' % (n, 'constant', 0.))
            elif p.dim() in [2, 3, 4]:
                nn.init.uniform_(p, a=-param_init, b=param_init)
                logger.info('Initialize %s with %s / %.3f' % (n, 'uniform', param_init))
            else:
                raise ValueError(n)

    def start_scheduled_sampling(self):
        self._ss_prob = self.ss_prob

    def forward(self, eouts, elens, ys, task='all', ys_hist=[],
                teacher_logits=None, recog_params={}):
        """Forward computation.

        Args:
            eouts (FloatTensor): `[B, T, enc_n_units]`
            elens (IntTensor): `[B]`
            ys (list): A list of length `[B]`, which contains a list of size `[L]`
            task (str): all/ys*/ys_sub*
            ys_hist (list):
            teacher_logits (FloatTensor): `[B, L, vocab]`
            recog_params (dict): parameters for MBR training
        Returns:
            loss (FloatTensor): `[1]`
            observation (dict):

        """
        observation = {'loss': None, 'loss_att': None, 'loss_ctc': None, 'loss_mbr': None,
                       'acc_att': None, 'ppl_att': None}
        loss = eouts.new_zeros((1,))

        # if self.lm is not None:
        #     self.lm.eval()

        # CTC loss
        if self.ctc_weight > 0 and (task == 'all' or 'ctc' in task):
            loss_ctc, trigger_points = self.ctc(eouts, elens, ys,
                                                forced_align=self.mocha_ctc_sync and self.training)
            observation['loss_ctc'] = loss_ctc.item()
            if self.mtl_per_batch:
                loss += loss_ctc
            else:
                loss += loss_ctc * self.ctc_weight * self.loss_scale
        else:
            trigger_points = None

        # XE loss
        if self.att_weight > 0 and (task == 'all' or 'ctc' not in task):
            loss_att, acc_att, ppl_att, loss_qua, loss_lat = self.forward_att(
                eouts, elens, ys, ys_hist, teacher_logits=teacher_logits,
                trigger_points=trigger_points)
            observation['loss_att'] = loss_att.item()
            observation['acc_att'] = acc_att
            observation['ppl_att'] = ppl_att
            if self.quantity_loss_weight > 0:
                loss_att += loss_qua * self.quantity_loss_weight
                observation['loss_quantity'] = loss_qua.item()
            if self.mocha_ctc_sync in ['decot', 'minlt']:
                observation['loss_latency'] = loss_lat.item() if self.training else 0
                if self.mocha_ctc_sync == 'minlt':
                    loss_att += loss_lat * 1.0
            if self.mtl_per_batch:
                loss += loss_att
            else:
                loss += loss_att * self.att_weight * self.loss_scale

        # MBR loss
        if self.mbr_weight > 0 and (task == 'all' or 'mbr' not in task):
            recog_params['recog_beam_width'] = self.mbr_nbest
            recog_params['recog_softmax_smoothing'] = self.mbr_softmax_smoothing
            loss_mbr = 0.
            for b in range(eouts.size(0)):
                self.eval()
                with torch.no_grad():
                    # 1. beam search
                    nbest_hyps_id, _, scores = self.beam_search(
                        eouts[b:b + 1], elens[b:b + 1], params=recog_params,
                        nbest=self.mbr_nbest, exclude_eos=True)
                    nbest_hyps_id_b = pad_list([np2tensor(np.fromiter(y, dtype=np.int64), self.device_id)
                                                for y in nbest_hyps_id[0]], self.pad)
                    eos = eouts.new_zeros(1).fill_(self.eos).long()
                    nbest_hyps_id_b_eos = pad_list([torch.cat([np2tensor(np.fromiter(y, dtype=np.int64), self.device_id),
                                                               eos], dim=0)
                                                    for y in nbest_hyps_id[0]], self.pad)
                    scores_b = np2tensor(np.array(scores[0], dtype=np.float32), self.device_id)
                    scores_b_norm = scores_b / scores_b.sum()

                    # 2. calculate expected WER
                    risks_b = np2tensor(np.array([editdistance.eval(ys[b], nbest_hyps_id_b[n])
                                                  for n in range(self.mbr_nbest)], dtype=np.float32), self.device_id)
                    exp_risk_b = (scores_b_norm * risks_b).sum()
                    grad_b = (scores_b_norm * (risks_b - exp_risk_b)).sum()

                # 3. forward pass (feed hypotheses)
                self.train()
                logits_b = self.forward_mbr(eouts[b:b + 1].repeat([self.mbr_nbest, 1, 1]),
                                            elens[b:b + 1].repeat([self.mbr_nbest]),
                                            nbest_hyps_id_b)

                # 4. backward pass (attatch gradient)
                log_probs_b = torch.log_softmax(logits_b, dim=-1)
                loss_mbr += self.mbr(log_probs_b, nbest_hyps_id_b_eos, exp_risk_b, grad_b)

            loss += loss_mbr * self.mbr_weight
            observation['loss_mbr'] = loss_mbr.item()

        observation['loss'] = loss.item()
        return loss, observation

    def forward_att(self, eouts, elens, ys, ys_hist=[],
                    return_logits=False, teacher_logits=None, trigger_points=None):
        """Compute XE loss for the attention-based decoder.

        Args:
            eouts (FloatTensor): `[B, T, enc_n_units]`
            elens (IntTensor): `[B]`
            ys (list): A list of length `[B]`, which contains a list of size `[L]`
            ys_hist (list):
            return_logits (bool): return logits for knowledge distillation
            teacher_logits (FloatTensor): `[B, L, vocab]`
            trigger_points (IntTensor): `[B, T]`
        Returns:
            loss (FloatTensor): `[1]`
            acc (float): accuracy for token prediction
            ppl (float): perplexity
            loss_qua (FloatTensor): `[1]`
            loss_lat (FloatTensor): `[1]`

        """
        bs, xtime = eouts.size()[:2]

        # Append <sos> and <eos>
        ys_in, ys_out, ylens = append_sos_eos(eouts, ys, self.eos, self.eos, self.pad, self.bwd)

        # Initialization
        dstates = self.zero_state(bs)
        if self.discourse_aware == 'state_carry_over' and self.dstate_prev is not None:
            dstates['dstate']['hxs'], dstates['dstate']['cxs'] = self.dstate_prev
            self.dstate_prev = None
        cv = eouts.new_zeros(bs, 1, self.enc_n_units)
        self.score.reset()
        aw, aws = None, []
        lmout, lmstate = None, None

        ys_emb = self.dropout_emb(self.embed(ys_in))
        src_mask = make_pad_mask(elens, self.device_id).unsqueeze(1)  # `[B, 1, T]`
        logits = []
        for t in range(ys_in.size(1)):
            is_sample = t > 0 and self._ss_prob > 0 and random.random() < self._ss_prob

            # Update LM states for LM fusion
            if self.lm is not None:
                y_lm = self.output(logits[-1]).detach().argmax(-1) if is_sample else ys_in[:, t:t + 1]
                lmout, lmstate = self.lm.decode(y_lm, lmstate)

            # Recurrency -> Score -> Generate
            y_emb = self.dropout_emb(self.embed(
                self.output(logits[-1]).detach().argmax(-1))) if is_sample else ys_emb[:, t:t + 1]
            dstates, cv, aw, attn_v = self.decode_step(
                eouts, dstates, cv, y_emb, src_mask, aw, lmout, mode='parallel',
                trigger_point=trigger_points[:, t] if (trigger_points is not None and self.mocha_ctc_sync == 'decot') else None)
            aws.append(aw.transpose(2, 1).unsqueeze(2))  # `[B, n_heads, 1, T]`
            logits.append(attn_v)

            if self.discourse_aware == 'state_carry_over':
                if self.dstate_prev is None:
                    self.dstate_prev = ([None] * bs, [None] * bs)
                if t in ylens.tolist():
                    for b in ylens.tolist().index(t):
                        self.dstate_prev[0][b] = dstates['dstate']['hxs'][b:b + 1].detach()
                        if self.dec_type == 'lstm':
                            self.dstate_prev[1][b] = dstates['dstate']['cxs'][b:b + 1].detach()

        if self.discourse_aware == 'state_carry_over':
            self.dstate_prev[0] = torch.cat(self.dstate_prev[0], dim=1)
            if self.dec_type == 'lstm':
                self.dstate_prev[1] = torch.cat(self.dstate_prev[1], dim=1)

        logits = self.output(torch.cat(logits, dim=1))

        # for knowledge distillation
        if return_logits:
            return logits

        # for attention plot
        aws = torch.cat(aws, dim=2)  # `[B, n_heads, L, T]`
        if not self.training:
            self.aws = tensor2np(aws)

        # Compute XE sequence loss (+ label smoothing)
        loss, ppl = cross_entropy_lsm(logits, ys_out, self.lsm_prob, self.pad, self.training)

        if self.quantity_loss_weight > 0 or (trigger_points is not None and self.mocha_ctc_sync in ['decot', 'minlt']):
            assert self.score.n_heads == 1
            aws = aws.squeeze(1)  # `[B, L, T]`
            tgt_mask = make_pad_mask(ylens, self.device_id).unsqueeze(2)  # `[B, L, 1]`
            aws = aws.masked_fill_(src_mask == 0, 0)
            aws = aws.masked_fill_(tgt_mask == 0, 0)

        # Quantity loss
        loss_qua = 0.
        if self.quantity_loss_weight > 0:
            assert self.attn_type in ['mocha', 'gmm']
            n_tokens_pred = aws.sum(2).sum(1)
            n_tokens_ref = (ys_out != self.pad).sum(1).float()
            loss_qua = torch.mean(torch.abs(n_tokens_pred - n_tokens_ref))
            # NOTE: this setting counts <eos> tokens

        # Latency loss
        loss_lat = 0.
        if trigger_points is not None and self.mocha_ctc_sync in ['decot', 'minlt']:
            time_indices = torch.arange(xtime).repeat([bs, ys_in.size(1), 1]).float().cuda(self.device_id)
            exp_trigger_points = (time_indices * aws).sum(2)  # `[B, L]`
            trigger_points = trigger_points.float().cuda(self.device_id)  # `[B, L]`
            # minimize the difference
            loss_lat = torch.mean(torch.abs(exp_trigger_points - trigger_points))
            # NOTE: trigger_points are padded with 0

        # Knowledge distillation
        if teacher_logits is not None:
            kl_loss = distillation(logits, teacher_logits, ylens, temperature=5.0)
            loss = loss * (1 - self.soft_label_weight) + kl_loss * self.soft_label_weight

        # Compute token-level accuracy in teacher-forcing
        acc = compute_accuracy(logits, ys_out, self.pad)

        return loss, acc, ppl, loss_qua, loss_lat

    def forward_mbr(self, eouts, elens, ys):
        """Compute XE loss for the attention-based decoder.

        Args:
            eouts (FloatTensor): `[nbest, T, enc_n_units]`
            elens (IntTensor): `[nbest]`
            ys (list): A list of length `[nbest]`, which contains a list of size `[L]`
        Returns:
            logits (FloatTensor): `[nbest, L, vocab]`

        """
        bs, xtime = eouts.size()[:2]

        # Append <sos> and <eos>
        ys_in, ys_out, ylens = append_sos_eos(eouts, ys, self.eos, self.eos, self.pad, self.bwd)

        # Initialization
        dstates = self.zero_state(bs)
        cv = eouts.new_zeros(bs, 1, self.enc_n_units)
        self.score.reset()
        aw, aws = None, []
        lmout, lmstate = None, None

        ys_emb = self.dropout_emb(self.embed(ys_in))
        src_mask = make_pad_mask(elens, self.device_id)
        logits = []
        for t in range(ys_in.size(1)):
            is_sample = t > 0 and self._ss_prob > 0 and random.random() < self._ss_prob

            # Update LM states for LM fusion
            if self.lm is not None:
                y_lm = self.output(logits[-1]).detach().argmax(-1) if is_sample else ys_in[:, t:t + 1]
                lmout, lmstate = self.lm.decode(y_lm, lmstate)

            # Recurrency -> Score -> Generate
            y_emb = self.dropout_emb(self.embed(
                self.output(logits[-1]).detach().argmax(-1))) if is_sample else ys_emb[:, t:t + 1]
            dstates, cv, aw, attn_v = self.decode_step(
                eouts, dstates, cv, y_emb, src_mask, aw, lmout, mode='parallel')
            aws.append(aw.transpose(2, 1).unsqueeze(2))  # `[B, n_heads, 1, T]`
            logits.append(attn_v)

        logits = self.output(torch.cat(logits, dim=1))
        return logits

    def decode_step(self, eouts, dstates, cv, y_emb, mask, aw, lmout,
                    mode='hard', cache=True, trigger_point=None):
        dstates = self.recurrency(torch.cat([y_emb, cv], dim=-1), dstates['dstate'])
        cv, aw = self.score(eouts, eouts, dstates['dout_score'], mask, aw,
                            mode, cache, trigger_point)
        attn_v = self.generate(cv, dstates['dout_gen'], lmout)
        return dstates, cv, aw, attn_v

    def zero_state(self, bs):
        """Initialize decoder state.

        Args:
            bs (int): batch size
        Returns:
            dstates (dict):
                dout (FloatTensor): `[B, 1, dec_n_units]`
                dstate (tuple): A tuple of (hxs, cxs)
                    hxs (FloatTensor): `[n_layers, B, dec_n_units]`
                    cxs (FloatTensor): `[n_layers, B, dec_n_units]`

        """
        dstates = {'dstate': None}
        w = next(self.parameters())
        hxs = w.new_zeros(self.n_layers, bs, self.dec_n_units)
        cxs = w.new_zeros(self.n_layers, bs, self.dec_n_units) if self.rnn_type == 'lstm' else None
        dstates['dstate'] = (hxs, cxs)
        return dstates

    def recurrency(self, inputs, dstate):
        """Recurrency function.

        Args:
            inputs (FloatTensor): `[B, 1, emb_dim + enc_n_units]`
            dstate (tuple): A tuple of (hxs, cxs)
        Returns:
            new_dstates (dict):
                dout_score (FloatTensor): `[B, 1, dec_n_units]`
                dout_gen (FloatTensor): `[B, 1, dec_n_units]`
                dstate (tuple): A tuple of (hxs, cxs)
                    hxs (FloatTensor): `[n_layers, B, dec_n_units]`
                    cxs (FloatTensor): `[n_layers, B, dec_n_units]`

        """
        hxs, cxs = dstate
        dout = inputs.squeeze(1)

        new_dstates = {'dout_score': None,  # for attention scoring
                       'dout_gen': None,  # for token generation
                       'dstate': None}

        new_hxs, new_cxs = [], []
        for l in range(self.n_layers):
            if self.rnn_type == 'lstm':
                h, c = self.rnn[l](dout, (hxs[l], cxs[l]))
                new_cxs.append(c)
            elif self.rnn_type == 'gru':
                h = self.rnn[l](dout, hxs[l])
            new_hxs.append(h)
            dout = self.dropout(h)
            if self.n_projs > 0:
                dout = torch.tanh(self.proj[l](dout))
            # use output in the first layer for attention scoring
            if l == 0:
                new_dstates['dout_score'] = dout.unsqueeze(1)
        new_hxs = torch.stack(new_hxs, dim=0)
        if self.rnn_type == 'lstm':
            new_cxs = torch.stack(new_cxs, dim=0)

        # use oupput in the the last layer for label generation
        new_dstates['dout_gen'] = dout.unsqueeze(1)
        new_dstates['dstate'] = (new_hxs, new_cxs)
        return new_dstates

    def generate(self, cv, dout, lmout):
        """Generate function.

        Args:
            cv (FloatTensor): `[B, 1, enc_n_units]`
            dout (FloatTensor): `[B, 1, dec_n_units]`
            lmout (FloatTensor): `[B, 1, lm_n_units]`
        Returns:
            attn_v (FloatTensor): `[B, 1, vocab]`

        """
        gated_lmfeat = None
        if self.lm is not None:
            # LM fusion
            dec_feat = self.linear_dec_feat(torch.cat([dout, cv], dim=-1))

            if self.lm_fusion_type in ['cold', 'deep']:
                lmfeat = self.linear_lm_feat(lmout)
                gate = torch.sigmoid(self.linear_lm_gate(torch.cat([dec_feat, lmfeat], dim=-1)))
                gated_lmfeat = gate * lmfeat
            elif self.lm_fusion_type == 'cold_prob':
                lmfeat = self.linear_lm_feat(self.lm.output(lmout))
                gate = torch.sigmoid(self.linear_lm_gate(torch.cat([dec_feat, lmfeat], dim=-1)))
                gated_lmfeat = gate * lmfeat

            out = self.output_bn(torch.cat([dec_feat, gated_lmfeat], dim=-1))
        else:
            out = self.output_bn(torch.cat([dout, cv], dim=-1))
        attn_v = torch.tanh(out)
        return attn_v

    def _plot_attention(self, save_path, n_cols=1):
        """Plot attention for each head."""
        from matplotlib import pyplot as plt
        from matplotlib.ticker import MaxNLocator

        _save_path = mkdir_join(save_path, 'dec_att_weights')

        # Clean directory
        if _save_path is not None and os.path.isdir(_save_path):
            shutil.rmtree(_save_path)
            os.mkdir(_save_path)

        if hasattr(self, 'aws'):
            plt.clf()
            fig, axes = plt.subplots(max(1, self.score.n_heads // n_cols), n_cols,
                                     figsize=(20, 8), squeeze=False)
            for h in range(self.score.n_heads):
                ax = axes[h // n_cols, h % n_cols]
                ax.imshow(self.aws[-1,  h, :, :], aspect="auto")
                ax.grid(False)
                ax.set_xlabel("Input (head%d)" % h)
                ax.set_ylabel("Output (head%d)" % h)
                ax.xaxis.set_major_locator(MaxNLocator(integer=True))
                ax.yaxis.set_major_locator(MaxNLocator(integer=True))

            fig.tight_layout()
            fig.savefig(os.path.join(_save_path, 'attention.png'), dvi=500)
            plt.close()

    def greedy(self, eouts, elens, max_len_ratio, idx2token,
               exclude_eos=False, refs_id=None, utt_ids=None, speakers=None):
        """Greedy decoding.

        Args:
            eouts (FloatTensor): `[B, T, enc_units]`
            elens (IntTensor): `[B]`
            max_len_ratio (int): maximum sequence length of tokens
            idx2token (): converter from index to token
            exclude_eos (bool): exclude <eos> from hypothesis
            refs_id (list): reference list
            utt_ids (list): utterance id list
            speakers (list): speaker list
        Returns:
            hyps (list): A list of length `[B]`, which contains arrays of size `[L]`
            aws (list): A list of length `[B]`, which contains arrays of size `[n_heads, L, T]`

        """
        bs, xtime, _ = eouts.size()

        # Initialization
        dstates = self.zero_state(bs)
        cv = eouts.new_zeros(bs, 1, self.enc_n_units)
        self.score.reset()
        aw = None
        lmout, lmstate = None, None
        y = eouts.new_zeros(bs, 1).fill_(refs_id[0][0] if self.replace_sos else self.eos).long()

        # Create the attention mask
        src_mask = make_pad_mask(elens, self.device_id)

        hyps_batch, aws_batch = [], []
        ylens = torch.zeros(bs).int()
        eos_flags = [False] * bs
        ytime = int(math.floor(xtime * max_len_ratio)) + 1
        for t in range(ytime):
            # Update LM states for LM fusion
            if self.lm is not None:
                lmout, lmstate = self.lm.decode(self.lm(y), lmstate)

            # Recurrency -> Score -> Generate
            y_emb = self.dropout_emb(self.embed(y))
            dstates, cv, aw, attn_v = self.decode_step(eouts, dstates, cv, y_emb, src_mask, aw, lmout)
            aws_batch += [aw.transpose(2, 1).unsqueeze(2)]  # `[B, n_heads, 1, T]`

            # Pick up 1-best
            y = self.output(attn_v).argmax(-1)
            hyps_batch += [y]

            # Count lengths of hypotheses
            for b in range(bs):
                if not eos_flags[b]:
                    if y[b].item() == self.eos:
                        eos_flags[b] = True
                    ylens[b] += 1  # include <eos>

            # Break if <eos> is outputed in all mini-batch
            if sum(eos_flags) == bs:
                break
            if t == ytime - 1:
                break

        # LM state carry over
        self.lmstate_final = lmstate

        # Concatenate in L dimension
        hyps_batch = tensor2np(torch.cat(hyps_batch, dim=1))
        aws_batch = tensor2np(torch.cat(aws_batch, dim=2))  # `[B, n_heads, L, T]`

        # Truncate by the first <eos> (<sos> in case of the backward decoder)
        if self.bwd:
            # Reverse the order
            hyps = [hyps_batch[b, :ylens[b]][::-1] for b in range(bs)]
            aws = [aws_batch[b, :, :ylens[b]][::-1] for b in range(bs)]
        else:
            hyps = [hyps_batch[b, :ylens[b]] for b in range(bs)]
            aws = [aws_batch[b, :, :ylens[b]] for b in range(bs)]

        # Exclude <eos> (<sos> in case of the backward decoder)
        if exclude_eos:
            if self.bwd:
                hyps = [hyps[b][1:] if eos_flags[b] else hyps[b] for b in range(bs)]
            else:
                hyps = [hyps[b][:-1] if eos_flags[b] else hyps[b] for b in range(bs)]

        for b in range(bs):
            if utt_ids is not None:
                logger.debug('Utt-id: %s' % utt_ids[b])
            if refs_id is not None and self.vocab == idx2token.vocab:
                logger.debug('Ref: %s' % idx2token(refs_id[b]))
            if self.bwd:
                logger.debug('Hyp: %s' % idx2token(hyps[b][::-1]))
            else:
                logger.debug('Hyp: %s' % idx2token(hyps[b]))

        return hyps, aws

    def beam_search(self, eouts, elens, params, idx2token=None,
                    lm=None, lm_second=None, lm_second_rev=None, ctc_log_probs=None,
                    nbest=1, exclude_eos=False,
                    refs_id=None, utt_ids=None, speakers=None,
                    ensmbl_eouts=None, ensmbl_elens=None, ensmbl_decs=[]):
        """Beam search decoding.

        Args:
            eouts (FloatTensor): `[B, T, enc_n_units]`
            elens (IntTensor): `[B]`
            params (dict):
                recog_beam_width (int): size of beam
                recog_max_len_ratio (int): maximum sequence length of tokens
                recog_min_len_ratio (float): minimum sequence length of tokens
                recog_length_penalty (float): length penalty
                recog_coverage_penalty (float): coverage penalty
                recog_coverage_threshold (float): threshold for coverage penalty
                recog_lm_weight (float): weight of LM score
            idx2token (): converter from index to token
            lm: firsh path LM
            lm_second: second path LM
            lm_second_rev: secoding path backward LM
            ctc_log_probs (FloatTensor):
            nbest (int):
            exclude_eos (bool): exclude <eos> from hypothesis
            refs_id (list): reference list
            utt_ids (list): utterance id list
            speakers (list): speaker list
            ensmbl_eouts (list): list of FloatTensor
            ensmbl_elens (list) list of list
            ensmbl_decs (list): list of torch.nn.Module
        Returns:
            nbest_hyps_idx (list): A list of length `[B]`, which contains list of N hypotheses
            aws (list): A list of length `[B]`, which contains arrays of size `[L, T]`
            scores (list):

        """
        bs, xmax, _ = eouts.size()
        n_models = len(ensmbl_decs) + 1

        beam_width = params['recog_beam_width']
        assert 1 <= nbest <= beam_width
        ctc_weight = params['recog_ctc_weight']
        max_len_ratio = params['recog_max_len_ratio']
        min_len_ratio = params['recog_min_len_ratio']
        lp_weight = params['recog_length_penalty']
        cp_weight = params['recog_coverage_penalty']
        cp_threshold = params['recog_coverage_threshold']
        length_norm = params['recog_length_norm']
        lm_weight = params['recog_lm_weight']
        lm_weight_second = params['recog_lm_second_weight']
        lm_weight_second_bwd = params['recog_lm_bwd_weight']
        gnmt_decoding = params['recog_gnmt_decoding']
        eos_threshold = params['recog_eos_threshold']
        asr_state_carry_over = params['recog_asr_state_carry_over']
        lm_state_carry_over = params['recog_lm_state_carry_over']
        softmax_smoothing = params['recog_softmax_smoothing']

        if lm is not None:
            assert lm_weight > 0
            lm.eval()
        if lm_second is not None:
            assert lm_weight_second > 0
            lm_second.eval()
        if lm_second_rev is not None:
            assert lm_weight_second_bwd > 0
            lm_second_rev.eval()

        if ctc_log_probs is not None:
            assert ctc_weight > 0
            ctc_log_probs = tensor2np(ctc_log_probs)

        nbest_hyps_idx, aws, scores = [], [], []
        eos_flags = []
        for b in range(bs):
            # Initialization per utterance
            self.score.reset()
            dstates = self.zero_state(1)
            lmstate = None

            # For joint CTC-Attention decoding
            ctc_prefix_scorer = None
            if ctc_log_probs is not None:
                if self.bwd:
                    ctc_prefix_scorer = CTCPrefixScore(ctc_log_probs[b][::-1], self.blank, self.eos)
                else:
                    ctc_prefix_scorer = CTCPrefixScore(ctc_log_probs[b], self.blank, self.eos)

            # Ensemble initialization
            ensmbl_dstate, ensmbl_cv = [], []
            if n_models > 1:
                for dec in ensmbl_decs:
                    ensmbl_dstate += [dec.zero_state(1)]
                    ensmbl_cv += [eouts.new_zeros(1, 1, dec.enc_n_units)]
                    dec.score.reset()

            if speakers is not None:
                if speakers[b] == self.prev_spk:
                    if asr_state_carry_over:
                        dstates = self.dstates_final
                    if lm_state_carry_over and isinstance(lm, RNNLM):
                        lmstate = self.lmstate_final
                self.prev_spk = speakers[b]

            helper = BeamSearch(beam_width, self.eos, ctc_weight, self.device_id)

            end_hyps = []
            hyps = [{'hyp': [self.eos],
                     'score': 0.,
                     'score_attn': 0.,
                     'score_ctc': 0.,
                     'score_lm': 0.,
                     'dstates': dstates,
                     'cv': eouts.new_zeros(1, 1, self.enc_n_units),
                     'aws': [None],
                     'lmstate': lmstate,
                     'ensmbl_dstate': ensmbl_dstate,
                     'ensmbl_cv': ensmbl_cv,
                     'ensmbl_aws':[[None]] * (n_models - 1),
                     'ctc_state': ctc_prefix_scorer.initial_state() if ctc_prefix_scorer is not None else None}]
            ytime = int(math.floor(elens[b] * max_len_ratio)) + 1
            for t in range(ytime):
                # preprocess for batch decoding
                y = eouts.new_zeros(len(hyps), 1).long()
                for j, beam in enumerate(hyps):
                    if self.replace_sos and t == 0:
                        prev_idx = refs_id[0][0]
                    else:
                        prev_idx = beam['hyp'][-1]
                    y[j, 0] = prev_idx
                cv = torch.cat([beam['cv'] for beam in hyps], dim=0)
                aw = torch.cat([beam['aws'][-1] for beam in hyps], dim=0) if t > 0 else None
                hxs = torch.cat([beam['dstates']['dstate'][0] for beam in hyps], dim=1)
                if self.rnn_type == 'lstm':
                    cxs = torch.cat([beam['dstates']['dstate'][1] for beam in hyps], dim=1)
                dstates = {'dstate': (hxs, cxs)}

                # Update LM states for LM fusion
                lmout, lmstate, scores_lm = None, None, None
                if lm is not None or self.lm is not None:
                    if beam['lmstate'] is not None:
                        lm_hxs = torch.cat([beam['lmstate']['hxs'] for beam in hyps], dim=1)
                        lm_cxs = torch.cat([beam['lmstate']['cxs'] for beam in hyps], dim=1)
                        lmstate = {'hxs': lm_hxs, 'cxs': lm_cxs}
                    if self.lm is not None:
                        # cold/deep fusion
                        lmout, lmstate, scores_lm = self.lm.predict(y, lmstate)
                    elif lm is not None:
                        # shallow fusion
                        lmout, lmstate, scores_lm = lm.predict(y, lmstate)

                # for the main model
                dstates, cv, aw, attn_v = self.decode_step(
                    eouts[b:b + 1, :elens[b]].repeat([cv.size(0), 1, 1]),
                    dstates, cv, self.dropout_emb(self.embed(y)), None, aw, lmout)
                probs = torch.softmax(self.output(attn_v).squeeze(1) * softmax_smoothing, dim=1)

                # for the ensemble
                ensmbl_dstate, ensmbl_cv, ensmbl_aws = [], [], []
                if n_models > 1:
                    for i_e, dec in enumerate(ensmbl_decs):
                        cv_e = torch.cat([beam['ensmbl_cv'][i_e] for beam in hyps], dim=0)
                        aw_e = torch.cat([beam['ensmbl_aws'][i_e][-1] for beam in hyps], dim=0) if t > 0 else None
                        hxs_e = torch.cat([beam['ensmbl_dstate'][i_e]['dstate'][0] for beam in hyps], dim=1)
                        if self.rnn_type == 'lstm':
                            cxs_e = torch.cat([beam['dstates'][i_e]['dstate'][1] for beam in hyps], dim=1)
                        dstates_e = {'dstate': (hxs_e, cxs_e)}

                        dstate_e, cv_e, aw_e, attn_v_e = dec.decode_step(
                            ensmbl_eouts[i_e][b:b + 1, :ensmbl_elens[i_e][b]].repeat([cv_e.size(0), 1, 1]),
                            dstates_e, cv_e, dec.dropout_emb(dec.embed(y)), None, aw_e, lmout)

                        ensmbl_dstate += [{'dstate': (beam['dstates'][i_e]['dstate'][0][:, j:j + 1],
                                                      beam['dstates'][i_e]['dstate'][1][:, j:j + 1])}]
                        ensmbl_cv += [cv_e[j:j + 1]]
                        ensmbl_aws += [beam['ensmbl_aws'][i_e] + [aw_e[j:j + 1]]]
                        probs += torch.softmax(dec.output(attn_v_e).squeeze(1), dim=1)
                        # NOTE: sum in the probability scale (not log-scale)

                # Ensemble in log-scale
                scores_attn = torch.log(probs) / n_models

                new_hyps = []
                for j, beam in enumerate(hyps):
                    # Attention scores
                    total_scores_attn = beam['score_attn'] + scores_attn[j:j + 1]
                    total_scores = total_scores_attn * (1 - ctc_weight)

                    # Add LM score <after> top-K selection
                    total_scores_topk, topk_ids = torch.topk(
                        total_scores, k=beam_width, dim=1, largest=True, sorted=True)
                    if lm is not None:
                        total_scores_lm = beam['score_lm'] + scores_lm[j, -1, topk_ids[0]]
                        total_scores_topk += total_scores_lm * lm_weight
                    else:
                        total_scores_lm = eouts.new_zeros(beam_width)

                    # Add length penalty
                    if lp_weight > 0:
                        if gnmt_decoding:
                            lp = math.pow(6 + len(beam['hyp'][1:]), lp_weight) / math.pow(6, lp_weight)
                            total_scores_topk /= lp
                        else:
                            total_scores_topk += (len(beam['hyp'][1:]) + 1) * lp_weight

                    # Add coverage penalty
                    if cp_weight > 0:
                        aw_mat = torch.stack(beam['aws'][1:] + [aw], dim=-1)  # `[B, T, L, n_heads]`
                        aw_mat = aw_mat[:, :, :, 0]
                        if gnmt_decoding:
                            aw_mat = torch.log(aw_mat.sum(-1))
                            cp = torch.where(aw_mat < 0, aw_mat, aw_mat.new_zeros(aw_mat.size())).sum()
                            # TODO(hirofumi): mask by elens[b]
                            total_scores_topk += cp * cp_weight
                        else:
                            # Recompute converage penalty at each step
                            if cp_threshold == 0:
                                cp = aw_mat.sum() / self.score.n_heads
                            else:
                                cp = torch.where(aw_mat > cp_threshold, aw_mat,
                                                 aw_mat.new_zeros(aw_mat.size())).sum() / self.score.n_heads
                            total_scores_topk += cp * cp_weight
                    else:
                        cp = 0.

                    # Add CTC score
                    new_ctc_states, total_scores_ctc, total_scores_topk = helper.add_ctc_score(
                        beam['hyp'], topk_ids, beam['ctc_state'],
                        total_scores_topk, ctc_prefix_scorer)

                    for k in range(beam_width):
                        idx = topk_ids[0, k].item()
                        length_norm_factor = 1.
                        if length_norm:
                            length_norm_factor = len(beam['hyp'][1:]) + 1
                        total_score = total_scores_topk[0, k].item() / length_norm_factor

                        if idx == self.eos:
                            # Exclude short hypotheses
                            if len(beam['hyp']) - 1 < elens[b] * min_len_ratio:
                                continue
                            # EOS threshold
                            max_score_no_eos = scores_attn[j, :idx].max(0)[0].item()
                            max_score_no_eos = max(max_score_no_eos, scores_attn[j, idx + 1:].max(0)[0].item())
                            if scores_attn[j, idx].item() <= eos_threshold * max_score_no_eos:
                                continue

                        new_hyps.append(
                            {'hyp': beam['hyp'] + [idx],
                             'score': total_score,
                             'score_attn': total_scores_attn[0, idx].item(),
                             'score_cp': cp,
                             'score_ctc': total_scores_ctc[k].item(),
                             'score_lm': total_scores_lm[k].item(),
                             'dstates': {'dstate': (dstates['dstate'][0][:, j:j + 1], dstates['dstate'][1][:, j:j + 1])},
                             'cv': cv[j:j + 1],
                             'aws': beam['aws'] + [aw[j:j + 1]],
                             'lmstate': {'hxs': lmstate['hxs'][:, j:j + 1], 'cxs': lmstate['cxs'][:, j:j + 1]} if lmstate is not None else None,
                             'ctc_state': new_ctc_states[k] if ctc_prefix_scorer is not None else None,
                             'ensmbl_dstate': ensmbl_dstate,
                             'ensmbl_cv': ensmbl_cv,
                             'ensmbl_aws': ensmbl_aws})

                # Local pruning
                new_hyps_sorted = sorted(new_hyps, key=lambda x: x['score'], reverse=True)[:beam_width]

                # Remove complete hypotheses
                new_hyps, end_hyps, is_finish = helper.remove_complete_hyp(new_hyps_sorted, end_hyps)
                hyps = new_hyps[:]
                if is_finish:
                    break

            # Global pruning
            if len(end_hyps) == 0:
                end_hyps = hyps[:]
            elif len(end_hyps) < nbest and nbest > 1:
                end_hyps.extend(hyps[:nbest - len(end_hyps)])

            # forward second path LM rescoring
            if lm_second is not None:
                self.lm_rescoring(end_hyps, lm_second, lm_weight_second, tag='second')

            # backward secodn path LM rescoring
            if lm_second_rev is not None:
                self.lm_rescoring(end_hyps, lm_second_rev, lm_weight_second_bwd, tag='second_rev')

            # Sort by score
            end_hyps = sorted(end_hyps, key=lambda x: x['score'], reverse=True)

            if utt_ids is not None:
                logger.info('Utt-id: %s' % utt_ids[b])
            if refs_id is not None and idx2token is not None and self.vocab == idx2token.vocab:
                logger.info('Ref: %s' % idx2token(refs_id[b]))
            if idx2token is not None:
                for k in range(len(end_hyps)):
                    logger.info('Hyp: %s' % idx2token(
                        end_hyps[k]['hyp'][1:][::-1] if self.bwd else end_hyps[k]['hyp'][1:]))
                    logger.info('log prob (hyp): %.7f' % end_hyps[k]['score'])
                    logger.info('log prob (hyp, att): %.7f' % (end_hyps[k]['score_attn'] * (1 - ctc_weight)))
                    logger.info('log prob (hyp, cp): %.7f' % (end_hyps[k]['score_cp'] * cp_weight))
                    if ctc_prefix_scorer is not None:
                        logger.info('log prob (hyp, ctc): %.7f' % (end_hyps[k]['score_ctc'] * ctc_weight))
                    if lm is not None:
                        logger.info('log prob (hyp, first-path lm): %.7f' % (end_hyps[k]['score_lm'] * lm_weight))
                    if lm_second is not None:
                        logger.info('log prob (hyp, second-path lm): %.7f' %
                                    (end_hyps[k]['score_lm_second'] * lm_weight_second))
                    if lm_second_rev is not None:
                        logger.info('log prob (hyp, second-path lm, reverse): %.7f' %
                                    (end_hyps[k]['score_lm_second_rev'] * lm_weight_second_bwd))

            # N-best list
            if self.bwd:
                # Reverse the order
                nbest_hyps_idx += [[np.array(end_hyps[n]['hyp'][1:][::-1]) for n in range(nbest)]]
                aws += [tensor2np(torch.stack(end_hyps[0]['aws'][1:][::-1], dim=1).squeeze(0))]
            else:
                nbest_hyps_idx += [[np.array(end_hyps[n]['hyp'][1:]) for n in range(nbest)]]
                aws += [tensor2np(torch.stack(end_hyps[0]['aws'][1:], dim=1).squeeze(0))]
            scores += [[end_hyps[n]['score_attn'] for n in range(nbest)]]

            # Check <eos>
            eos_flags.append([(end_hyps[n]['hyp'][-1] == self.eos) for n in range(nbest)])

        # Exclude <eos> (<sos> in case of the backward decoder)
        if exclude_eos:
            if self.bwd:
                nbest_hyps_idx = [[nbest_hyps_idx[b][n][1:] if eos_flags[b][n]
                                   else nbest_hyps_idx[b][n] for n in range(nbest)] for b in range(bs)]
            else:
                nbest_hyps_idx = [[nbest_hyps_idx[b][n][:-1] if eos_flags[b][n]
                                   else nbest_hyps_idx[b][n] for n in range(nbest)] for b in range(bs)]

        # Store ASR/LM state
        self.dstates_final = end_hyps[0]['dstates']
        self.lmstate_final = end_hyps[0]['lmstate']

        return nbest_hyps_idx, aws, scores

    def beam_search_chunk_sync(self, eouts_chunk, params, idx2token,
                               lm=None, lm_second=None, ctc_log_probs=None,
                               hyps=False, state_carry_over=False, ignore_eos=False):
        bs, chunk_size, enc_dim = eouts_chunk.size()
        assert bs == 1
        assert self.attn_type == 'mocha'

        beam_width = params['recog_beam_width']
        ctc_weight = params['recog_ctc_weight']
        max_len_ratio = params['recog_max_len_ratio']
        lp_weight = params['recog_length_penalty']
        lm_weight = params['recog_lm_weight']
        lm_weight_second = params['recog_lm_second_weight']
        eos_threshold = params['recog_eos_threshold']
        # ctc_spike_forced_decoding = params['recog_ctc_spike_forced_decoding']

        if lm is not None:
            assert lm_weight > 0
            lm.eval()
        if lm_second is not None:
            assert lm_weight_second > 0
            lm_second.eval()

        # Initialization per utterance
        self.score.reset()
        dstates = self.zero_state(1)
        lmstate = None

        # For joint CTC-Attention decoding
        if ctc_log_probs is not None:
            assert ctc_weight > 0
            ctc_log_probs = tensor2np(ctc_log_probs)
            if hyps is None:
                # first chunk
                self.ctc_prefix_scorer = CTCPrefixScore(ctc_log_probs[0], self.blank, self.eos)
            else:
                self.ctc_prefix_scorer.register_new_chunk(ctc_log_probs[0])
        else:
            self.ctc_prefix_scorer = None

        if state_carry_over:
            dstates = self.dstates_final
            if isinstance(lm, RNNLM):
                lmstate = self.lmstate_final

        helper = BeamSearch(beam_width, self.eos, ctc_weight, self.device_id)

        end_hyps = []
        if hyps is None:
            self.n_frames = 0
            self.chunk_size = eouts_chunk.size(1)
            hyps = [{'hyp': [self.eos],
                     'score': 0.,
                     'score_attn': 0.,
                     'score_ctc': 0.,
                     'score_lm': 0.,
                     'dstates': dstates,
                     'cv': eouts_chunk.new_zeros(1, 1, self.enc_n_units),
                     'aws': [None],
                     'lmstate': lmstate,
                     'ctc_state': self.ctc_prefix_scorer.initial_state() if self.ctc_prefix_scorer is not None else None,
                     'no_trigger': False}]

        ytime = int(math.floor(eouts_chunk.size(1) * max_len_ratio)) + 1
        n_hyps_prev = 1
        for t in range(ytime):
            # finish if additional triggered points are not found in all candidates
            if t > 0 and sum([cand['no_trigger'] for cand in hyps]) == len(hyps):
                break

            # ignore ended hypotheses from batch decoding
            new_hyps = []
            # hyps_filtered = []
            # for j, beam in enumerate(hyps):
            #     # no triggered point found in this chunk
            #     if beam['no_trigger'] == True:
            #         new_hyps.append(beam.copy())
            #     else:
            #         hyps_filtered.append(beam.copy())
            # if len(hyps_filtered) == 0:
            #     break
            # hyps = hyps_filtered[:]

            # preprocess for batch decoding
            y = eouts_chunk.new_zeros(len(hyps), 1).long()
            for j, beam in enumerate(hyps):
                y[j, 0] = beam['hyp'][-1]
            cv = torch.cat([beam['cv'] for beam in hyps], dim=0)
            aw = torch.cat([beam['aws'][-1] for beam in hyps], dim=0) if t > 0 else None
            hxs = torch.cat([beam['dstates']['dstate'][0] for beam in hyps], dim=1)
            if self.rnn_type == 'lstm':
                cxs = torch.cat([beam['dstates']['dstate'][1] for beam in hyps], dim=1)
            dstates = {'dstate': (hxs, cxs)}

            # Update LM states for LM fusion
            lmout, lmstate, scores_lm = None, None, None
            if lm is not None or self.lm is not None:
                if beam['lmstate'] is not None:
                    lm_hxs = torch.cat([beam['lmstate']['hxs'] for beam in hyps], dim=1)
                    lm_cxs = torch.cat([beam['lmstate']['cxs'] for beam in hyps], dim=1)
                    lmstate = {'hxs': lm_hxs, 'cxs': lm_cxs}
                if self.lm is not None:
                    # cold/deep fusion
                    lmout, lmstate, scores_lm = self.lm.predict(y, lmstate)
                elif lm is not None:
                    # shallow fusion
                    lmout, lmstate, scores_lm = lm.predict(y, lmstate)

            dstates, cv, aw, attn_v = self.decode_step(
                eouts_chunk[0:1].repeat([cv.size(0), 1, 1]),
                dstates, cv, self.dropout_emb(self.embed(y)), None, aw, lmout,
                cache=cv.size(0) == n_hyps_prev)
            scores_attn = torch.log_softmax(self.output(attn_v).squeeze(1), dim=1)
            n_hyps_prev = cv.size(0)

            for j, beam in enumerate(hyps):
                # no triggered point found in this chunk
                if aw[j].sum() == 0:
                    beam['aws'][-1] = eouts_chunk.new_zeros(eouts_chunk.size(0), eouts_chunk.size(1), 1)
                    # NOTE: for the case where the first token in the current chunk is <eos>
                    beam['no_trigger'] = True
                    new_hyps.append(beam.copy())
                    continue

                # Attention scores
                total_scores_attn = beam['score_attn'] + scores_attn[j:j + 1]
                total_scores = total_scores_attn * (1 - ctc_weight)

                # Add LM score <after> top-K selection
                total_scores_topk, topk_ids = torch.topk(
                    total_scores, k=beam_width, dim=1, largest=True, sorted=True)
                if lm is not None:
                    total_scores_lm = beam['score_lm'] + scores_lm[j, -1, topk_ids[0]]
                    total_scores_topk += total_scores_lm * lm_weight
                else:
                    total_scores_lm = eouts_chunk.new_zeros(beam_width)

                # Add length penalty
                total_scores_topk += (len(beam['hyp'][1:]) + 1) * lp_weight

                # Add CTC score
                new_ctc_states, total_scores_ctc, total_scores_topk = helper.add_ctc_score(
                    beam['hyp'], topk_ids, beam['ctc_state'],
                    total_scores_topk, self.ctc_prefix_scorer, new_chunk=(t == 0))

                topk_ids = [topk_ids[0, k].item() for k in range(beam_width)]

                for k in range(beam_width):
                    idx = topk_ids[k]
                    total_score = total_scores_topk[0, k].item() / (len(beam['hyp'][1:]) + 1)

                    if idx == self.eos:
                        if ignore_eos:
                            beam['aws'][-1] = eouts_chunk.new_zeros(eouts_chunk.size(0), eouts_chunk.size(1), 1)
                            # NOTE: for unidirectional encoder
                            beam['no_trigger'] = True
                            new_hyps.append(beam.copy())
                            continue
                        # EOS threshold
                        max_score_no_eos = scores_attn[j, :idx].max(0)[0].item()
                        max_score_no_eos = max(max_score_no_eos, scores_attn[j, idx + 1:].max(0)[0].item())
                        if scores_attn[j, idx].item() <= eos_threshold * max_score_no_eos:
                            continue

                    new_hyps.append(
                        {'hyp': beam['hyp'] + [idx],
                         'score': total_score,
                         'score_attn': total_scores_attn[0, idx].item(),
                         'score_ctc': total_scores_ctc[k].item(),
                         'score_lm': total_scores_lm[k].item(),
                         'dstates': {'dstate': (dstates['dstate'][0][:, j:j + 1], dstates['dstate'][1][:, j:j + 1])},
                         'cv': cv[j:j + 1],
                         'aws': beam['aws'] + [aw[j:j + 1]],
                         'lmstate': {'hxs': lmstate['hxs'][:, j:j + 1], 'cxs': lmstate['cxs'][:, j:j + 1]} if lmstate is not None else None,
                         'ctc_state': new_ctc_states[k] if self.ctc_prefix_scorer is not None else None,
                         'no_trigger': False})

            # Local pruning
            new_hyps_sorted = sorted(new_hyps, key=lambda x: x['score'], reverse=True)[:beam_width]

            # Remove complete hypotheses
            new_hyps, end_hyps, is_finish = helper.remove_complete_hyp(new_hyps_sorted, end_hyps)
            hyps = new_hyps[:]
            if is_finish:
                break

        # forward second path LM rescoring
        if lm_second is not None:
            self.lm_rescoring(end_hyps, lm_second, lm_weight_second, tag='second')
            # TODO: fix bug for empty hypotheses

        # Sort by score
        if len(end_hyps) > 0:
            end_hyps = sorted(end_hyps, key=lambda x: x['score'], reverse=True)

        merged_hyps = sorted(end_hyps + hyps, key=lambda x: x['score'], reverse=True)[:beam_width]
        for k in range(len(merged_hyps)):
            logger.info('Hyp: %s' % idx2token(merged_hyps[k]['hyp'][1:]))
            logger.info('log prob (hyp): %.7f' % merged_hyps[k]['score'])
            logger.info('log prob (hyp, att): %.7f' % (merged_hyps[k]['score_attn'] * (1 - ctc_weight)))
            if self.ctc_prefix_scorer is not None:
                logger.info('log prob (hyp, ctc): %.7f' % (merged_hyps[k]['score_ctc'] * ctc_weight))
            if lm is not None:
                logger.info('log prob (hyp, first-path lm): %.7f' % (merged_hyps[k]['score_lm'] * lm_weight))
            if lm_second is not None:
                logger.info('log prob (hyp, second-path lm): %.7f' %
                            (merged_hyps[k]['score_lm_second'] * lm_weight_second))

        aws = None
        # if len(end_hyps) > 0 and len(end_hyps[0]['aws'][1:]) > 0:
        #     if chunk_size != self.chunk_size:
        #         aws = eouts_chunk.new_zeros(len(end_hyps[0]['aws'][1:]), self.chunk_size, self.score.n_heads)
        #         for i, aw in enumerate(end_hyps[0]['aws'][-1]):
        #             aws[i:i + 1, :aw.size(0)] = aw.transpose(1, 0).unsqueeze(2)
        #         aws = tensor2np(aws)
        #     else:
        #         aws = tensor2np(torch.stack(end_hyps[0]['aws'][1:], dim=1).squeeze(0))

        # Store ASR/LM state
        if len(end_hyps) > 0:
            self.dstates_final = end_hyps[0]['dstates']
            self.lmstate_final = end_hyps[0]['lmstate']

        self.n_frames += eouts_chunk.size(1)

        return end_hyps, hyps, aws
