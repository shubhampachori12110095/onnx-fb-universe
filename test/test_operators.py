from test_pytorch_common import TestCase, run_tests, skipIfNoLapack, flatten
import test_onnx_common

import torch
import torch.onnx
from torch.autograd import Variable, Function
from torch.nn import Module
import torch.nn as nn

import onnx
import onnx.checker
import onnx.helper

import google.protobuf.text_format

import itertools
import io
import unittest
import inspect
import argparse
import glob
import os
import shutil
import sys
import common
from onnx import numpy_helper


_onnx_test = False


def export_to_string(model, inputs, *args, **kwargs):
    f = io.BytesIO()
    torch.onnx.export(model, inputs, f, *args, **kwargs)
    return f.getvalue()


class FuncModule(Module):
    def __init__(self, f, params=tuple()):
        super(FuncModule, self).__init__()
        self.f = f
        self.params = nn.ParameterList(list(params))

    def forward(self, *args):
        return self.f(*itertools.chain(args, self.params))


class TestOperators(TestCase):

    def assertONNXExpected(self, binary_pb, subname=None):
        model_def = onnx.ModelProto.FromString(binary_pb)
        onnx.checker.check_model(model_def)
        # doc_string contains stack trace in it, strip it
        onnx.helper.strip_doc_string(model_def)
        self.assertExpected(google.protobuf.text_format.MessageToString(model_def, float_format='.15g'), subname)
        return model_def

    def assertONNX(self, f, args, params=tuple(), **kwargs):
        if isinstance(f, nn.Module):
            m = f
        else:
            m = FuncModule(f, params)
        onnx_model_pb = export_to_string(m, args, **kwargs)
        model_def = self.assertONNXExpected(onnx_model_pb)
        if _onnx_test:
            test_function = inspect.stack()[1][0].f_code.co_name
            test_name = test_function[0:4] + "_operator" + test_function[4:]
            output_dir = os.path.join(test_onnx_common.pytorch_operator_dir, test_name)
            # Assume:
            #     1) the old test should be delete before the test.
            #     2) only one assertONNX in each test, otherwise will override the data.
            assert not os.path.exists(output_dir), "{} should not exist!".format(output_dir)
            os.makedirs(output_dir)
            with open(os.path.join(output_dir, "model.pb"), 'wb') as file:
                file.write(model_def.SerializeToString())
            data_dir = os.path.join(output_dir, "test_data_set_0")
            os.makedirs(data_dir)
            if isinstance(args, Variable):
                args = (args,)
            for index, var in enumerate(flatten(args)):
                tensor = numpy_helper.from_array(var.data.numpy())
                with open(os.path.join(data_dir, "input_{}.pb".format(index)), 'wb') as file:
                    file.write(tensor.SerializeToString())
            outputs = m(*args)
            if isinstance(outputs, Variable):
                outputs = (outputs,)
            for index, var in enumerate(flatten(outputs)):
                tensor = numpy_helper.from_array(var.data.numpy())
                with open(os.path.join(data_dir, "output_{}.pb".format(index)), 'wb') as file:
                    file.write(tensor.SerializeToString())

    def assertONNXRaises(self, err, f, args, params=tuple(), **kwargs):
        if isinstance(f, nn.Module):
            m = f
        else:
            m = FuncModule(f, params)
        self.assertExpectedRaises(err, lambda: export_to_string(m, args, **kwargs))

    def test_basic(self):
        x = Variable(torch.Tensor([0.4]), requires_grad=True)
        y = Variable(torch.Tensor([0.7]), requires_grad=True)
        self.assertONNX(lambda x, y: -torch.sigmoid(torch.tanh(x * (x + y))), (x, y))

    def test_view(self):
        x = Variable(torch.Tensor([0]), requires_grad=True)
        self.assertONNX(lambda x: x.view(1, 1), x)

    def test_index(self):
        x = Variable(torch.Tensor([[0]]), requires_grad=True)
        self.assertONNX(lambda x: x[0], x)

    def test_type_as(self):
        x = Variable(torch.Tensor([0]), requires_grad=True)
        self.assertONNX(lambda x: x.type_as(x), x)

    def test_addconstant(self):
        x = Variable(torch.DoubleTensor(2, 3), requires_grad=True)
        self.assertONNX(lambda x: x + 1, x)

    def test_add_broadcast(self):
        x = Variable(torch.DoubleTensor(2, 3), requires_grad=True)
        y = Variable(torch.DoubleTensor(3), requires_grad=True)
        self.assertONNX(lambda x, y: x + y, (x, y))

    def test_add_left_broadcast(self):
        x = Variable(torch.DoubleTensor(3), requires_grad=True)
        y = Variable(torch.DoubleTensor(2, 3), requires_grad=True)
        self.assertONNXRaises(RuntimeError, lambda x, y: x + y, (x, y))

    def test_add_size1_broadcast(self):
        x = Variable(torch.DoubleTensor(2, 3), requires_grad=True)
        y = Variable(torch.DoubleTensor(2, 1), requires_grad=True)
        self.assertONNXRaises(RuntimeError, lambda x, y: x + y, (x, y))

    def test_transpose(self):
        x = Variable(torch.Tensor([[0, 1], [2, 3]]), requires_grad=True)
        self.assertONNX(lambda x: x.transpose(0, 1).transpose(1, 0), x)

    def test_chunk(self):
        x = Variable(torch.Tensor([0,1,2]), requires_grad=True)
        self.assertONNX(lambda x: x.chunk(2), x)

    def test_concat2(self):
        # volatile is of particular interest; it caused a segfault
        # with the exporter
        x = Variable(torch.randn(2, 3), volatile=True)
        y = Variable(torch.randn(2, 3), volatile=True)
        self.assertONNX(lambda inputs: torch.cat(inputs, 1), ((x, y),))

    def test_mm(self):
        m1 = Variable(torch.randn(2, 3), requires_grad=True)
        m2 = Variable(torch.randn(3, 4), requires_grad=True)
        self.assertONNX(torch.mm, (m1, m2))

    def test_addmm(self):
        m1 = Variable(torch.randn(2, 3), requires_grad=True)
        m2 = Variable(torch.randn(3, 4), requires_grad=True)
        m3 = Variable(torch.randn(4), requires_grad=True)
        self.assertONNX(lambda x, y, z: torch.addmm(torch.addmm(z, x, y), x, y), (m1, m2, m3))

    def test_permute2(self):
        x = Variable(torch.Tensor([[[[[[0]]]]]]), requires_grad=True)
        self.assertONNX(lambda x: x.permute(0, 1, 4, 2, 5, 3), x)

    def test_pad(self):
        x = Variable(torch.Tensor([[[[0, 1, 1, 1], [2, 3, 7, 7]]]]), requires_grad=True)
        self.assertONNX(nn.ReflectionPad2d((3, 4, 1, 2)), x)

    def test_params(self):
        x = Variable(torch.Tensor([[1, 2], [3, 4]]), requires_grad=True)
        y = nn.Parameter(torch.Tensor([[1, 2], [3, 4]]), requires_grad=True)
        self.assertONNX(lambda x, y: -torch.sigmoid(torch.tanh(x * (x + y))), x, params=(y, ))

    def test_non_float_params(self):
        x = Variable(torch.LongTensor([[1, 2], [3, 4]]), requires_grad=True)
        y = nn.Parameter(torch.LongTensor([[1, 2], [3, 4]]), requires_grad=True)
        self.assertONNX(lambda x, y: x * (x + y), x, params=(y, ))

    def test_symbolic_mismatch(self):
        class MyFun(Function):
            @staticmethod
            def symbolic(g, x):
                # The inside of this function should never be invoked, because
                # we will fail due to an argument mismatch first.
                assert False

            @staticmethod
            def forward(ctx, x, y):
                return x + y

        x = Variable(torch.randn(2, 2).fill_(1.0))
        y = Variable(torch.randn(2, 2).fill_(1.0))
        # NB: Don't use expect test here, the type error wobbles depending
        # on Python version
        with self.assertRaisesRegex(TypeError, "occurred when translating MyFun"):
            export_to_string(FuncModule(MyFun().apply), (x, y))

    # TODO: Do an nn style test for these
    def test_batchnorm(self):
        x = Variable(torch.randn(2, 2).fill_(1.0), requires_grad=True)
        self.assertONNX(nn.BatchNorm2d(2), x)

    def test_batchnorm_training(self):
        x = Variable(torch.randn(2, 2).fill_(1.0), requires_grad=True)
        self.assertONNX(nn.BatchNorm2d(2), x, training=True)

    def test_conv(self):
        x = Variable(torch.randn(20, 16, 50, 40).fill_(1.0), requires_grad=True)
        self.assertONNX(nn.Conv2d(16, 13, 3, bias=False), x)

    def test_maxpool(self):
        x = Variable(torch.randn(20, 16, 50))
        self.assertONNX(nn.MaxPool1d(3, stride=2), x)

    def test_at_op(self):
        x = Variable(torch.randn(3, 4))

        class MyFun(Function):

            @staticmethod
            def symbolic(g, x):
                return g.at("add", x, x)

            @staticmethod
            def forward(ctx, x):
                return x + x

        class MyModule(Module):
            def forward(self, x):
                return MyFun.apply(x)

        self.assertONNX(MyModule(), x)

    def test_clip(self):
        x = Variable(torch.randn(3, 4), requires_grad=True)
        self.assertONNX(lambda x: torch.clamp(x, min=-0.5, max=0.5), x)

    def test_max(self):
        x = Variable(torch.randn(3, 4), requires_grad=True)
        y = Variable(torch.randn(3, 4), requires_grad=True)
        self.assertONNX(lambda x, y: torch.max(x, y), (x, y))

    def test_min(self):
        x = Variable(torch.randn(3, 4), requires_grad=True)
        y = Variable(torch.randn(3, 4), requires_grad=True)
        self.assertONNX(lambda x, y: torch.min(x, y), (x, y))

    def test_equal(self):
        x = Variable(torch.randn(3, 4).int(), requires_grad=True)
        y = Variable(torch.randn(3, 4).int(), requires_grad=True)
        self.assertONNX(lambda x, y: x == y, (x, y))

    def test_exp(self):
        x = Variable(torch.randn(3, 4), requires_grad=True)
        self.assertONNX(lambda x: x.exp(), x)

    def test_flatten(self):
        # Flatten is a special case of Reshape when the output is a 2-D tensor.
        x = Variable(torch.randn(1, 2, 3, 4), requires_grad=True)
        self.assertONNX(lambda x: x.view(x.size()[0], x.numel() // x.size()[0]), x)

    def test_logsoftmax(self):
        x = Variable(torch.randn(1, 2, 3, 4), requires_grad=True)
        self.assertONNX(nn.LogSoftmax(dim=2), x)

    def test_pow(self):
        x = Variable(torch.randn(1, 2, 3, 4), requires_grad=True)
        y = Variable(torch.randn(1, 2, 3, 4), requires_grad=True)
        self.assertONNX(lambda x, y: x.pow(y), (x, y))

    def test_selu(self):
        x = Variable(torch.randn(1, 2, 3, 4), requires_grad=True)
        self.assertONNX(nn.SELU(), x)

if __name__ == '__main__':
    onnx_test_flag = '--onnx-test'
    _onnx_test = onnx_test_flag in common.UNITTEST_ARGS
    if onnx_test_flag in common.UNITTEST_ARGS:
        common.UNITTEST_ARGS.remove(onnx_test_flag)
    if _onnx_test:
        for d in glob.glob(os.path.join(test_onnx_common.pytorch_operator_dir, "test_operator_*")):
            shutil.rmtree(d)
    run_tests()
