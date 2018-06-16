import torch
from torch.autograd import Variable
from torch.autograd import Function
import torch.nn.functional as F
import torch.nn as nn
from linalg_utils import pdist2, PDist2Order
from collections import namedtuple
import pytorch_utils as pt_utils
from typing import List, Tuple

import pointnet


class RandomDropout(nn.Module):

    def __init__(self, p=0.5, inplace=False):
        super().__init__()
        self.p = p
        self.inplace = inplace

    def forward(self, X):
        theta = torch.Tensor(1).uniform_(0, self.p)[0]
        return pt_utils.feature_dropout_no_scaling(
            X, theta, self.train, self.inplace
        )


class FurthestPointSampling(Function):

    @staticmethod
    def forward(ctx, xyz: torch.Tensor, npoint: int) -> torch.Tensor:
        r"""
        Uses iterative furthest point sampling to select a set of npoint points that have the largest
        minimum distance

        Parameters
        ----------
        xyz : torch.Tensor
            (B, N, 3) tensor where N > npoint
        npoint : int32
            number of points in the sampled set

        Returns
        -------
        torch.Tensor
            (B, npoint) tensor containing the set
        """
        output = torch.cuda.IntTensor(xyz.size(0), npoint)

        output, = pointnet.furthest_point_sampling(npoint, xyz, output)

        return output

    @staticmethod
    def backward(xyz, a=None):
        return None, None


furthest_point_sample = FurthestPointSampling.apply


class GatherPoints(Function):

    @staticmethod
    def forward(ctx, points: torch.Tensor, idx: torch.Tensor) -> torch.Tensor:
        r"""

        Parameters
        ----------
        points : torch.Tensor
            (B, C, N) tensor

        idx : torch.Tensor
            (B, npoint) tensor of the points to gather

        Returns
        -------
        torch.Tensor
            (B, C, npoint) tensor
        """

        B, C, N = points.size()
        output = torch.cuda.FloatTensor(B, C, idx.size(1))

        output, = pointnet.gather_points(points, idx, output)

        ctx.for_backwards = (idx, C, N)

        return output

    @staticmethod
    def backward(ctx, grad_out):
        idx, C, N = ctx.for_backwards
        B, npoint = idx.size()

        grad_points = torch.cuda.FloatTensor(B, C, N)
        grad_points, = pointnet.gather_points_grad(grad_out, idx, grad_points)
        print(grad_points)

        return grad_points, None


gather_points = GatherPoints.apply


class ThreeNN(Function):

    @staticmethod
    def forward(ctx, unknown: torch.Tensor,
                known: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        r"""
            Find the three nearest neighbors of unknown in known
        Parameters
        ----------
        unknown : torch.Tensor
            (B, n, 3) tensor of known points
        known : torch.Tensor
            (B, m, 3) tensor of unknown points

        Returns
        -------
        dist : torch.Tensor
            (B, n, 3) l2 distance to the three nearest neighbors
        idx : torch.Tensor
            (B, n, 3) index of 3 nearest neighbors
        """
        assert unknown.is_contiguous()
        assert known.is_contiguous()

        B, N, _ = unknown.size()
        m = known.size(1)
        dist2 = torch.cuda.FloatTensor(B, N, 3)
        idx = torch.cuda.IntTensor(B, N, 3)

        pointnet2.three_nn_wrapper(B, N, m, unknown, known, dist2, idx)

        return torch.sqrt(dist2), idx

    @staticmethod
    def backward(ctx, a=None, b=None):
        return None, None


three_nn = ThreeNN.apply


class ThreeInterpolate(Function):

    @staticmethod
    def forward(
            ctx, points: torch.Tensor, idx: torch.Tensor, weight: torch.Tensor
    ) -> torch.Tensor:
        r"""
            Performs weight linear interpolation on 3 points
        Parameters
        ----------
        points : torch.Tensor
            (B, c, m)  Points to be interpolated from
        idx : torch.Tensor
            (B, n, 3) three nearest neighbors of the target points in points
        weight : torch.Tensor
            (B, n, 3) weights

        Returns
        -------
        torch.Tensor
            (B, c, n) tensor of the interpolated points
        """
        assert points.is_contiguous()
        assert idx.is_contiguous()
        assert weight.is_contiguous()

        B, c, m = points.size()
        n = idx.size(1)

        ctx.three_interpolate_for_backward = (idx, weight, m)

        output = torch.cuda.FloatTensor(B, c, n)

        pointnet2.three_interpolate_wrapper(
            B, c, m, n, points, idx, weight, output
        )

        return output

    @staticmethod
    def backward(ctx, grad_out: torch.Tensor
                ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        r"""
        Parameters
        ----------
        grad_out : torch.Tensor
            (B, c, n) tensor with gradients of ouputs

        Returns
        -------
        grad_points : torch.Tensor
            (B, c, m) tensor with gradients of points

        None

        None
        """
        idx, weight, m = ctx.three_interpolate_for_backward
        B, c, n = grad_out.size()

        grad_points = Variable(torch.cuda.FloatTensor(B, c, m).zero_())

        grad_out_data = grad_out.data.contiguous()
        pointnet2.three_interpolate_grad_wrapper(
            B, c, n, m, grad_out_data, idx, weight, grad_points.data
        )

        return grad_points, None, None


three_interpolate = ThreeInterpolate.apply


class GroupPoints(Function):

    @staticmethod
    def forward(ctx, points: torch.Tensor, idx: torch.Tensor) -> torch.Tensor:
        r"""

        Parameters
        ----------
        points : torch.Tensor
            (B, C, N) tensor of points to group
        idx : torch.Tensor
            (B, npoint, nsample) tensor containing the indicies of points to group with

        Returns
        -------
        torch.Tensor
            (B, C, npoint, nsample) tensor
        """
        assert points.is_contiguous()
        assert idx.is_contiguous()

        B, npoints, nsample = idx.size()
        _, C, N = points.size()

        output = torch.cuda.FloatTensor(B, C, npoints, nsample)

        pointnet2.group_points_wrapper(
            B, C, N, npoints, nsample, points, idx, output
        )

        ctx.for_backwards = (idx, N)
        return output

    @staticmethod
    def backward(ctx,
                 grad_out: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        r"""

        Parameters
        ----------
        grad_out : torch.Tensor
            (B, C, npoint, nsample) tensor of the gradients of the output from forward

        Returns
        -------
        torch.Tensor
            (B, C, N) gradient of the points
        None
        """
        idx, N = ctx.for_backwards

        B, C, npoint, nsample = grad_out.size()
        grad_points = Variable(torch.cuda.FloatTensor(B, C, N).zero_())

        grad_out_data = grad_out.data.contiguous()
        pointnet2.group_points_grad_wrapper(
            B, C, N, npoint, nsample, grad_out_data, idx, grad_points.data
        )

        return grad_points, None


group_points = GroupPoints.apply


class BallQuery(Function):

    @staticmethod
    def forward(
            ctx, radius: float, nsample: int, xyz: torch.Tensor,
            new_xyz: torch.Tensor
    ) -> torch.Tensor:
        r"""

        Parameters
        ----------
        radius : float
            radius of the balls
        nsample : int
            maximum number of points in the balls
        xyz : torch.Tensor
            (B, N, 3) xyz coordinates of the points
        new_xyz : torch.Tensor
            (B, npoint, 3) centers of the ball query

        Returns
        -------
        torch.Tensor
            (B, npoint, nsample) tensor with the indicies of the points that form the query balls
        """
        idx = torch.cuda.IntTensor(xyz.size(0), new_xyz.size(1), 3)
        idx, = pointnet.ball_query(radius, nsample, xyz, new_xyz, idx)
        return idx

    @staticmethod
    def backward(ctx, a=None):
        return None, None, None, None


ball_query = BallQuery.apply


class QueryAndGroup(nn.Module):
    r"""
    Groups with a ball query of radius

    Parameters
    ---------
    radius : float32
        Radius of ball
    nsample : int32
        Maximum number of points to gather in the ball
    """

    def __init__(self, radius: float, nsample: int, use_xyz: bool = True):
        super().__init__()
        self.radius, self.nsample, self.use_xyz = radius, nsample, use_xyz

    def forward(
            self,
            xyz: torch.Tensor,
            new_xyz: torch.Tensor,
            points: torch.Tensor = None
    ) -> Tuple[torch.Tensor]:
        r"""
        Parameters
        ----------
        xyz : torch.Tensor
            xyz coordinates of the points (B, N, 3)
        new_xyz : torch.Tensor
            centriods (B, npoint, 3)
        points : torch.Tensor
            Descriptors of the points (B, C, N)

        Returns
        -------
        new_points : torch.Tensor
            (B, 3 + C, npoint, nsample) tensor
        """

        idx = ball_query(self.radius, self.nsample, xyz, new_xyz)
        xyz_trans = xyz.transpose(1, 2).contiguous()
        grouped_xyz = group_points(xyz_trans, idx)  # (B, 3, npoint, nsample)
        grouped_xyz -= new_xyz.transpose(1, 2).unsqueeze(-1)

        if points is not None:
            grouped_points = group_points(points, idx)
            if self.use_xyz:
                new_points = torch.cat([grouped_xyz, grouped_points],
                                       dim=1)  # (B, C + 3, npoint, nsample)
            else:
                new_points = group_points
        else:
            new_points = grouped_xyz

        return new_points


class GroupAll(nn.Module):
    r"""
    Groups all points

    Parameters
    ---------
    """

    def __init__(self, use_xyz: bool = True):
        super().__init__()
        self.use_xyz = use_xyz

    def forward(
            self,
            xyz: torch.Tensor,
            new_xyz: torch.Tensor,
            points: torch.Tensor = None
    ) -> Tuple[torch.Tensor]:
        r"""
        Parameters
        ----------
        xyz : torch.Tensor
            xyz coordinates of the points (B, N, 3)
        new_xyz : torch.Tensor
            Ignored
        points : torch.Tensor
            Descriptors of the points (B, C, N)

        Returns
        -------
        new_points : torch.Tensor
            (B, C + 3, 1, N) tensor
        """

        grouped_xyz = xyz.transpose(1, 2).unsqueeze(2)
        if points is not None:
            grouped_points = points.unsqueeze(2)
            if self.use_xyz:
                new_points = torch.cat([grouped_xyz, grouped_points],
                                       dim=1)  # (B, 3 + C, 1, N)
            else:
                new_points = group_points
        else:
            new_points = grouped_xyz

        return new_points


if __name__ == "__main__":
    import numpy as np
    xyz = Variable(torch.cuda.FloatTensor(16, 1024, 3).uniform_(), requires_grad=True)
    tmp = furthest_point_sample(xyz, 5)
    print(tmp)
