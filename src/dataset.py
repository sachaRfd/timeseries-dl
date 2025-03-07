"""
Code regarding dataset setup + Dataloader + Collate functions
"""

import torch
from torch.utils.data import Dataset, DataLoader
import numpy as np


def geom_noise_mask_single(L, lm, masking_ratio):
    """
    Randomly create a boolean mask of length `L`, consisting of subsequences of average length lm, masking with 0s a `masking_ratio`
    proportion of the sequence L. The length of masking subsequences and intervals follow a geometric distribution.
    Args:
        L: length of mask and sequence to be masked
        lm: average length of masking subsequences (streaks of 0s)
        masking_ratio: proportion of L to be masked

    Returns:
        (L,) boolean numpy array intended to mask ('drop') with 0s a sequence of length L
    """
    keep_mask = np.ones(L, dtype=bool)
    p_m = (
        1 / lm
    )  # probability of each masking sequence stopping. parameter of geometric distribution.
    p_u = (
        p_m * masking_ratio / (1 - masking_ratio)
    )  # probability of each unmasked sequence stopping. parameter of geometric distribution.
    p = [p_m, p_u]

    # Start in state 0 with masking_ratio probability
    state = int(
        np.random.rand() > masking_ratio
    )  # state 0 means masking, 1 means not masking
    for i in range(L):
        keep_mask[i] = (
            state  # here it happens that state and masking value corresponding to state are identical
        )
        if np.random.rand() < p[state]:
            state = 1 - state

    return keep_mask


def noise_mask(
    X,
    masking_ratio,
    lm=3,
    mode="separate",
    distribution="geometric",
    exclude_feats=None,
):
    """
    Creates a random boolean mask of the same shape as X, with 0s at places where a feature should be masked.
    Args:
        X: (seq_length, feat_dim) numpy array of features corresponding to a single sample
        masking_ratio: proportion of seq_length to be masked. At each time step, will also be the proportion of
            feat_dim that will be masked on average
        lm: average length of masking subsequences (streaks of 0s). Used only when `distribution` is 'geometric'.
        mode: whether each variable should be masked separately ('separate'), or all variables at a certain positions
            should be masked concurrently ('concurrent')
        distribution: whether each mask sequence element is sampled independently at random, or whether
            sampling follows a markov chain (and thus is stateful), resulting in geometric distributions of
            masked squences of a desired mean length `lm`
        exclude_feats: iterable of indices corresponding to features to be excluded from masking (i.e. to remain all 1s)

    Returns:
        boolean numpy array with the same shape as X, with 0s at places where a feature should be masked
    """
    if exclude_feats is not None:
        exclude_feats = set(exclude_feats)

    if distribution == "geometric":  # stateful (Markov chain)
        if mode == "separate":  # each variable (feature) is independent
            mask = np.ones(X.shape, dtype=bool)
            for m in range(X.shape[1]):  # feature dimension
                if exclude_feats is None or m not in exclude_feats:
                    mask[:, m] = geom_noise_mask_single(
                        X.shape[0], lm, masking_ratio
                    )  # time dimension
        else:  # replicate across feature dimension (mask all variables at the same positions concurrently)
            mask = np.tile(
                np.expand_dims(
                    geom_noise_mask_single(X.shape[0], lm, masking_ratio), 1
                ),
                X.shape[1],
            )
    else:  # each position is independent Bernoulli with p = 1 - masking_ratio
        if mode == "separate":
            mask = np.random.choice(
                np.array([True, False]),
                size=X.shape,
                replace=True,
                p=(1 - masking_ratio, masking_ratio),
            )
        else:
            mask = np.tile(
                np.random.choice(
                    np.array([True, False]),
                    size=(X.shape[0], 1),
                    replace=True,
                    p=(1 - masking_ratio, masking_ratio),
                ),
                X.shape[1],
            )

    return mask


def compensate_masking(X, mask):
    """
    Compensate feature vectors after masking values, in a way that the matrix product W @ X would not be affected on average.
    If p is the proportion of unmasked (active) elements, X' = X / p = X * feat_dim/num_active
    Args:
        X: (batch_size, seq_length, feat_dim) torch tensor
        mask: (batch_size, seq_length, feat_dim) torch tensor: 0s means mask and predict, 1s: unaffected (active) input
    Returns:
        (batch_size, seq_length, feat_dim) compensated features
    """

    # number of unmasked elements of feature vector for each time step
    num_active = torch.sum(mask, dim=-1).unsqueeze(-1)  # (batch_size, seq_length, 1)
    # to avoid division by 0, set the minimum to 1
    num_active = torch.max(
        num_active, torch.ones(num_active.shape, dtype=torch.int16)
    )  # (batch_size, seq_length, 1)
    return X.shape[-1] * X / num_active


def padding_mask(lengths, max_len=None):
    """
    Used to mask padded positions: creates a (batch_size, max_len) boolean mask from a tensor of sequence lengths,
    where 1 means keep element at this position (time step)
    """
    batch_size = lengths.numel()
    max_len = (
        max_len or lengths.max_val()
    )  # trick works because of overloading of 'or' operator for non-boolean types
    return (
        torch.arange(0, max_len, device=lengths.device)
        .type_as(lengths)
        .repeat(batch_size, 1)
        .lt(lengths.unsqueeze(1))
    )


def collate_unsuperv(data, max_len=None, mask_compensation=False):
    """Build mini-batch tensors from a list of (X, mask) tuples. Mask input. Create
    Args:
        data: len(batch_size) list of tuples (X, mask).
            - X: torch tensor of shape (seq_length, feat_dim); variable seq_length.
            - mask: boolean torch tensor of shape (seq_length, feat_dim); variable seq_length.
        max_len: global fixed sequence length. Used for architectures requiring fixed length input,
            where the batch length cannot vary dynamically. Longer sequences are clipped, shorter are padded with 0s
    Returns:
        X: (batch_size, padded_length, feat_dim) torch tensor of masked features (input)
        targets: (batch_size, padded_length, feat_dim) torch tensor of unmasked features (output)
        target_masks: (batch_size, padded_length, feat_dim) boolean torch tensor
            0 indicates masked values to be predicted, 1 indicates unaffected/"active" feature values
        padding_masks: (batch_size, padded_length) boolean tensor, 1 means keep vector at this position, 0 ignore (padding)
    """

    batch_size = len(data)
    features, masks, labels = zip(*data)

    # Stack and pad features and masks (convert 2D to 3D tensors, i.e. add batch dimension)
    lengths = [
        X.shape[0] for X in features
    ]  # original sequence length for each time series
    if max_len is None:
        max_len = max(lengths)
    X = torch.zeros(
        batch_size, max_len, features[0].shape[-1]
    )  # (batch_size, padded_length, feat_dim)
    target_masks = torch.zeros_like(
        X, dtype=torch.bool
    )  # (batch_size, padded_length, feat_dim) masks related to objective
    for i in range(batch_size):
        end = min(lengths[i], max_len)
        X[i, :end, :] = features[i][:end, :]
        target_masks[i, :end, :] = masks[i][:end, :]

    targets = X.clone()
    X = X * target_masks  # mask input
    if mask_compensation:
        X = compensate_masking(X, target_masks)

    padding_masks = padding_mask(
        torch.tensor(lengths, dtype=torch.int16), max_len=max_len
    )  # (batch_size, padded_length) boolean tensor, "1" means keep
    target_masks = ~target_masks  # inverse logic: 0 now means ignore, 1 means predict
    return X, targets, target_masks, padding_masks, labels


class ImputationDataset(Dataset):
    """Dynamically computes missingness (noise) mask for each sample"""

    def __init__(
        self,
        data,
        mean_mask_length=3,
        masking_ratio=0.15,
        mode="separate",
        distribution="geometric",
        exclude_feats=None,
    ):
        super(ImputationDataset, self).__init__()

        # self.data = data  # this is a subclass of the BaseData class in data.py
        self.data = data["samples"]
        self.labels = data["labels"]

        # Normalise features:
        self.means = self.data.mean(dim=[0, 1])
        self.stds = self.data.std(dim=[0, 1])

        # Normalize
        self.data = (self.data - self.means) / self.stds

        self.masking_ratio = masking_ratio
        self.mean_mask_length = mean_mask_length
        self.mode = mode
        self.distribution = distribution
        self.exclude_feats = exclude_feats

    def __getitem__(self, ind):
        """
        For a given integer index, returns the corresponding (seq_length, feat_dim) array and a noise mask of same shape
        Args:
            ind: integer index of sample in dataset
        Returns:
            X: (seq_length, feat_dim) tensor of the multivariate time series corresponding to a sample
            mask: (seq_length, feat_dim) boolean tensor: 0s mask and predict, 1s: unaffected input
            ID: ID of sample
        """
        X = self.data[ind].numpy()
        label = self.labels[ind]

        # Create a RandomMask
        mask = noise_mask(
            X,
            self.masking_ratio,
            self.mean_mask_length,
            self.mode,
            self.distribution,
            self.exclude_feats,
        )  # (seq_length, feat_dim) boolean array

        return torch.from_numpy(X), torch.from_numpy(mask), label

    def __len__(self):
        return self.data.shape[0]
