"""Tests for attribute.py."""


from pytype import abstract
from pytype import config
from pytype import errors
from pytype import load_pytd
from pytype import vm

import unittest


class AttributeTest(unittest.TestCase):

  def setUp(self):
    options = config.Options.create()
    self._vm = vm.VirtualMachine(
        errors.ErrorLog(), options, load_pytd.Loader(None, options))

  def test_type_parameter_instance(self):
    t = abstract.TypeParameter(abstract.T, self._vm)
    t_instance = abstract.TypeParameterInstance(
        t, self._vm.convert.primitive_class_instances[str], self._vm)
    node, var = self._vm.attribute_handler.get_attribute(
        self._vm.root_cfg_node, t_instance, "upper")
    self.assertIs(node, self._vm.root_cfg_node)
    attr, = var.data
    self.assertIsInstance(attr, abstract.PyTDFunction)

  def test_type_parameter_instance_bad_attribute(self):
    t = abstract.TypeParameter(abstract.T, self._vm)
    t_instance = abstract.TypeParameterInstance(
        t, self._vm.convert.primitive_class_instances[str], self._vm)
    node, var = self._vm.attribute_handler.get_attribute(
        self._vm.root_cfg_node, t_instance, "rumpelstiltskin")
    self.assertIs(node, self._vm.root_cfg_node)
    self.assertIsNone(var)

  def test_empty_type_parameter_instance(self):
    t = abstract.TypeParameter(
        abstract.T, self._vm, bound=self._vm.convert.int_type)
    instance = abstract.Instance(self._vm.convert.list_type, self._vm)
    instance.initialize_type_parameter(
        self._vm.root_cfg_node, abstract.T, self._vm.program.NewVariable())
    t_instance = abstract.TypeParameterInstance(t, instance, self._vm)
    node, var = self._vm.attribute_handler.get_attribute(
        self._vm.root_cfg_node, t_instance, "real")
    self.assertIs(node, self._vm.root_cfg_node)
    attr, = var.data
    self.assertIs(attr, self._vm.convert.primitive_class_instances[int])


if __name__ == "__main__":
  unittest.main()
