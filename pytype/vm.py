"""A abstract virtual machine for python bytecode that generates typegraphs.

A VM for python byte code that uses pytype/pytd/cfg ("typegraph") to generate a
trace of the program execution.
"""

# We have names like "byte_NOP":
# pylint: disable=invalid-name

# Bytecodes don't always use all their arguments:
# pylint: disable=unused-argument

# VirtualMachine uses late initialization for its "frame" attribute:
# pytype: disable=none-attr

import collections
import logging
import os
import re
import repr as reprlib
import sys


from pytype import abc_overlay
from pytype import abstract
from pytype import annotations_util
from pytype import attribute
from pytype import blocks
from pytype import collections_overlay
from pytype import compare
from pytype import convert
from pytype import debug  # pylint: disable=unused-import
from pytype import directors
from pytype import exceptions
from pytype import function
from pytype import load_pytd
from pytype import matcher
from pytype import metrics
from pytype import special_builtins
from pytype import state as frame_state
from pytype import typing
from pytype import utils
from pytype.pyc import loadmarshal
from pytype.pyc import opcodes
from pytype.pyc import pyc
from pytype.pyi import parser
from pytype.pytd import cfg as typegraph
from pytype.pytd import mro
from pytype.pytd import slots
from pytype.pytd.parse import builtins
from pytype.pytd.parse import visitors

log = logging.getLogger(__name__)

_FUNCTION_TYPE_COMMENT_RE = re.compile(r"^\((.*)\)\s*->\s*(\S.*?)\s*$")

# Create a repr that won't overflow.
_TRUNCATE = 120
_TRUNCATE_STR = 72
repr_obj = reprlib.Repr()
repr_obj.maxother = _TRUNCATE
repr_obj.maxstring = _TRUNCATE_STR
repper = repr_obj.repr


Block = collections.namedtuple("Block", ["type", "op", "handler", "level"])

_opcode_counter = metrics.MapCounter("vm_opcode")

# Collection of module overlays, used in _import_module to fetch an overlay
# instead of the module itself. Memoized in the vm itself.
overlays = {
    "abc": abc_overlay.ABCOverlay,
    "collections": collections_overlay.CollectionsOverlay,
    "typing": typing.TypingOverlay,
}


class RecursionException(Exception):
  pass


class VirtualMachineError(Exception):
  """For raising errors in the operation of the VM."""
  pass


class _FindIgnoredTypeComments(object):
  """A visitor that finds type comments that will be ignored."""

  def __init__(self, type_comments):
    # Build sets of all lines with the associated style of type comment.
    # Lines will be removed from these sets during visiting.  Any lines
    # that remain at the end are type comments that will be ignored.
    self._ignored_function_lines = set()
    self._ignored_type_lines = set()
    for line, comment in type_comments.items():
      if comment[0]:
        self._ignored_type_lines.add(line)
      else:
        self._ignored_function_lines.add(line)
    # Keep a copy of the lines that have function comments.  This is necessary
    # because the function comment check involves finding the first line
    # within a range that contains a comment.  If the same code object is
    # referenced by multiple MAKE_FUNCTION ops, then simply using the ignored
    # set when checking for existence of a comment would be incorrect.
    self._function_lines = set(self._ignored_function_lines)

  def visit_code(self, code):
    """Interface for pyc.visit."""
    for i, op in enumerate(code.co_code):
      if isinstance(op, (opcodes.STORE_NAME,
                         opcodes.STORE_FAST,
                         opcodes.STORE_ATTR,
                         opcodes.STORE_GLOBAL)):
        self._ignored_type_lines.discard(op.line)
      elif isinstance(op, opcodes.MAKE_FUNCTION):
        code_line = self._find_code_line(code, i)
        if code_line is not None:
          # Discard the first function type comment line.
          for line in range(op.line + 1, code_line):
            if line in self._function_lines:
              self._ignored_function_lines.discard(line)
              break
    return code

  def _find_code_line(self, code, index):
    """Return the line number for the first opcode (or None).

    Args:
      code: An OrderedCode object.
      index: The index of the MAKE_FUNCTION op within code.co_code.

    Returns:
      The line number of the first opcode in the body of the function, or
      None if this cannot be determined.
    """
    if index < 1:
      return
    op = code.co_code[index-1]
    if op.name != "LOAD_CONST":
      return
    target_code = code.co_consts[op.arg]
    # If the object doesn't have a co_code attribute, or the co_code
    # attribute is an empty list, we can't determine the line.
    if not getattr(target_code, "co_code", None):
      return
    return target_code.co_code[0].line

  def ignored_lines(self):
    """Returns a set of lines that contain ignored type comments."""
    return self._ignored_function_lines | self._ignored_type_lines


class VirtualMachine(object):
  """A bytecode VM that generates a typegraph as it executes.

  Attributes:
    program: The typegraph.Program used to build the typegraph.
    root_cfg_node: The root CFG node that contains the definitions of builtins.
    primitive_classes: A mapping from primitive python types to their abstract
      types.
  """

  def __init__(self,
               errorlog,
               options,
               loader,
               module_name=None,
               generate_unknowns=False,
               analyze_annotated=False,
               cache_unknowns=True,
               store_all_calls=False):
    """Construct a TypegraphVirtualMachine."""
    self.maximum_depth = sys.maxint
    self.errorlog = errorlog
    self.options = options
    self.python_version = options.python_version
    self.generate_unknowns = generate_unknowns
    self.analyze_annotated = analyze_annotated
    self.cache_unknowns = cache_unknowns
    self.store_all_calls = store_all_calls
    self.loader = loader
    self.frames = []  # The call stack of frames.
    self.functions_with_late_annotations = []
    self.frame = None  # The current frame.
    self.program = typegraph.Program()
    self.root_cfg_node = self.program.NewCFGNode("root")
    self.program.entrypoint = self.root_cfg_node
    self.annotations_util = annotations_util.AnnotationsUtil(self)
    self.attribute_handler = attribute.AbstractAttributeHandler(self)
    self.convert = convert.Converter(self)
    self.program.default_data = self.convert.unsolvable
    self.matcher = matcher.AbstractMatcher()
    self.has_unknown_wildcard_imports = False
    self.callself_stack = []
    self.filename = None
    self.director = None
    self.reading_builtins = False

    # Map from builtin names to canonical objects.
    self.special_builtins = {
        # The super() function.
        "super": self.convert.super_type,
        # The object type.
        "object": self.convert.object_type,
        # for more pretty branching tests.
        "__random__": self.convert.primitive_class_instances[bool],
        # for debugging
        "reveal_type": special_builtins.RevealType(self),
        # boolean values.
        "True": self.convert.true,
        "False": self.convert.false,
        "isinstance": special_builtins.IsInstance(self),
        "issubclass": special_builtins.IsSubclass(self),
        "hasattr": special_builtins.HasAttr(self),
    }

    # Memoize which overlays are loaded.
    self.loaded_overlays = {}

  def lookup_builtin(self, name):
    try:
      return self.loader.builtins.Lookup(name)
    except KeyError:
      return self.loader.typing.Lookup(name)

  def remaining_depth(self):
    return self.maximum_depth - len(self.frames)

  def is_at_maximum_depth(self):
    return len(self.frames) > self.maximum_depth

  def run_instruction(self, op, state):
    """Run a single bytecode instruction.

    Args:
      op: An opcode, instance of pyc.opcodes.Opcode
      state: An instance of state.FrameState, the state just before running
        this instruction.
    Returns:
      A tuple (why, state). "why" is the reason (if any) that this opcode aborts
      this function (e.g. through a 'raise'), or None otherwise. "state" is the
      FrameState right after this instruction that should roll over to the
      subsequent instruction.
    """
    _opcode_counter.inc(op.name)
    self.frame.current_opcode = op
    if log.isEnabledFor(logging.INFO):
      self.log_opcode(op, state)
    try:
      # dispatch
      bytecode_fn = getattr(self, "byte_%s" % op.name, None)
      if bytecode_fn is None:
        raise VirtualMachineError("Unknown opcode: %s" % op.name)
      state = bytecode_fn(state, op)
    except RecursionException as e:
      # This is not an error - it just means that the block we're analyzing
      # goes into a recursion, and we're already two levels deep.
      state = state.set_why("recursion")
    except exceptions.ByteCodeException:
      e = sys.exc_info()[1]
      state = state.set_exception(e.exception_type, e.create_instance(), None)
      log.info("Exception in program: %s: %r",
               e.exception_type.__name__, e.message)
      state = state.set_why("exception")
    if state.why == "reraise":
      state = state.set_why("exception")
    self.frame.current_opcode = None
    return state

  def join_cfg_nodes(self, nodes):
    assert nodes
    if len(nodes) == 1:
      return nodes[0]
    else:
      ret = self.program.NewCFGNode(self.frame and
                                    self.frame.current_opcode and
                                    self.frame.current_opcode.line)
      for node in nodes:
        node.ConnectTo(ret)
      return ret

  def run_frame(self, frame, node):
    """Run a frame (typically belonging to a method)."""
    self.push_frame(frame)
    frame.states[frame.f_code.co_code[0]] = frame_state.FrameState.init(node,
                                                                        self)
    return_nodes = []
    for block in frame.f_code.order:
      state = frame.states.get(block[0])
      if not state:
        log.warning("Skipping block %d,"
                    " we don't have any non-erroneous code that goes here.",
                    block.id)
        continue
      op = None
      for op in block:
        state = self.run_instruction(op, state)
        if state.why:
          # we can't process this block any further
          break
      if state.why:
        # return, raise or yield. Leave the current frame.
        return_nodes.append(state.node)
      elif op.carry_on_to_next():
        # We're starting a new block, so start a new CFG node. We don't want
        # nodes to overlap the boundary of blocks.
        state = state.forward_cfg_node()
        frame.states[op.next] = state.merge_into(frame.states.get(op.next))
    self.pop_frame(frame)
    if not return_nodes:
      # Happens if the function never returns. (E.g. an infinite loop)
      assert not frame.return_variable.bindings
      frame.return_variable.AddBinding(self.convert.unsolvable, [], node)
    else:
      node = self.join_cfg_nodes(return_nodes)
    return node, frame.return_variable

  reversable_operators = set([
      "__add__", "__sub__", "__mul__",
      "__div__", "__truediv__", "__floordiv__",
      "__mod__", "__divmod__", "__pow__",
      "__lshift__", "__rshift__", "__and__", "__or__", "__xor__",
      "__matmul__"
  ])

  @staticmethod
  def reverse_operator_name(name):
    if name in VirtualMachine.reversable_operators:
      return "__r" + name[2:]
    return None

  def push_block(self, state, t, op, handler=None, level=None):
    if level is None:
      level = len(state.data_stack)
    return state.push_block(Block(t, op, handler, level))

  def push_frame(self, frame):
    self.frames.append(frame)
    self.frame = frame

  def pop_frame(self, frame):
    popped_frame = self.frames.pop()
    assert popped_frame == frame
    if self.frames:
      self.frame = self.frames[-1]
    else:
      self.frame = None

  def module_name(self):
    if self.frame.f_code.co_filename:
      return ".".join(re.sub(
          r"\.py$", "", self.frame.f_code.co_filename).split(os.sep)[-2:])
    else:
      return ""

  def log_opcode(self, op, state):
    """Write a multi-line log message, including backtrace and stack."""
    if not log.isEnabledFor(logging.INFO):
      return
    indent = " > " * (len(self.frames) - 1)
    stack_rep = repper(state.data_stack)
    block_stack_rep = repper(state.block_stack)
    module_name = self.module_name()
    if module_name:
      name = self.frame.f_code.co_name
      log.info("%s | index: %d, %r, module: %s line: %d",
               indent, op.index, name, module_name, op.line)
    else:
      log.info("%s | index: %d, line: %d",
               indent, op.index, op.line)
    log.info("%s | data_stack: %s", indent, stack_rep)
    log.info("%s | block_stack: %s", indent, block_stack_rep)
    log.info("%s | node: <%d>%s", indent, state.node.id, state.node.name)
    if log.isEnabledFor(logging.TRACE):
      log.trace("%s | backtrace: %s", indent,
                " ".join(str(f.current_opcode.line)
                         for f in self.frames
                         if isinstance(f, frame_state.Frame)))
    log.info("%s %s", indent, utils.maybe_truncate(str(op), _TRUNCATE))

  def repper(self, s):
    return repr_obj.repr(s)

  # Operators

  def pop_slice_and_obj(self, state, count):
    """Pop a slice from the data stack. Used by slice opcodes (SLICE_0 etc.)."""
    if count == 0:  # x[:]
      start = None
      end = None
    elif count == 1:  # x[i:]
      state, start = state.pop()
      end = None
    elif count == 2:  # x[:j]
      start = None
      state, end = state.pop()
    elif count == 3:  # x[i:j]
      state, end = state.pop()
      state, start = state.pop()
    else:
      raise AssertionError("invalid slice code")
    state, obj = state.pop()
    return state, (start, end), obj

  def store_slice(self, state, count):
    state, slice_args, obj = self.pop_slice_and_obj(state, count)
    slice_obj = self.convert.build_slice(state.node, *slice_args)
    state, new_value = state.pop()
    state, f = self.load_attr(state, obj, "__setitem__")
    state, _ = self.call_function_with_state(state, f, (slice_obj, new_value))
    return state

  def delete_slice(self, state, count):
    state, slice_args, obj = self.pop_slice_and_obj(state, count)
    slice_obj = self.convert.build_slice(state.node, *slice_args)
    return self._delete_item(state, obj, slice_obj)

  def get_slice(self, state, count):
    """Common implementation of all GETSLICE+<n> opcodes."""
    state, (start, end), obj = self.pop_slice_and_obj(state, count)
    state, f = self.load_attr(state, obj, "__getslice__")
    if f and f.bindings:
      start = start or self.convert.build_int(state.node)
      end = end or self.convert.build_int(state.node)
      state, ret = self.call_function_with_state(state, f, (start, end))
    else:
      slice_obj = self.convert.build_slice(state.node, start, end)
      state, f = self.load_attr(state, obj, "__getitem__")
      state, ret = self.call_function_with_state(state, f, (slice_obj,))
    return state.push(ret)

  def do_raise(self, state, exc, cause):
    """Raise an exception. Used by byte_RAISE_VARARGS."""
    if exc is None:     # reraise
      exc_type, val, _ = state.last_exception
      if exc_type is None:
        return state.set_why("exception")
      else:
        return state.set_why("reraise")
    elif isinstance(exc, type):
      # As in `raise ValueError`
      exc_type = exc
      val = exc()       # Make an instance.
    elif isinstance(exc, BaseException):
      # As in `raise ValueError('foo')`
      exc_type = type(exc)
      val = exc
    else:
      return state

    # If you reach this point, you're guaranteed that
    # val is a valid exception instance and exc_type is its class.
    # Now do a similar thing for the cause, if present.
    if cause:
      if isinstance(cause, type):
        cause = cause()
      elif not isinstance(cause, BaseException):
        return state

      val.__cause__ = cause

    state.set_exception(exc_type, val, val.__traceback__)
    return state

  # Importing

  def join_variables(self, node, variables):
    return self.program.MergeVariables(node, variables)

  def join_bindings(self, node, bindings):
    return self.program.MergeBindings(node, bindings)

  def _process_base_class(self, node, base):
    """Process a base class for InterpreterClass creation."""
    new_base = self.program.NewVariable()
    for b in base.bindings:
      if isinstance(b.data, abstract.AnnotationContainer):
        new_base.AddBinding(b.data.base_cls, {b}, node)
      elif isinstance(b.data, abstract.Union):
        # Union[A,B,...] is a valid base class, but we need to flatten it into a
        # single base variable.
        for o in b.data.options:
          new_base.AddBinding(o, {b}, node)
      else:
        new_base.AddBinding(b.data, {b}, node)
    base = new_base
    if not any(isinstance(t, (abstract.Class, abstract.AMBIGUOUS_OR_EMPTY))
               for t in base.data):
      self.errorlog.base_class_error(self.frames, base)
    return base

  def make_class(self, node, name_var, bases, class_dict_var, cls_var):
    """Create a class with the name, bases and methods given.

    Args:
      node: The current CFG node.
      name_var: Class name.
      bases: Base classes.
      class_dict_var: Members of the class, as a Variable containing an
          abstract.Dict value.
      cls_var: The class's metaclass, if any.

    Returns:
      An instance of Class.
    """
    name = abstract.get_atomic_python_constant(name_var)
    log.info("Declaring class %s", name)
    try:
      class_dict = abstract.get_atomic_value(class_dict_var)
    except abstract.ConversionError:
      log.error("Error initializing class %r", name)
      return self.convert.create_new_unknown(node)
    # Flatten Unions in the bases
    bases = [self._process_base_class(node, base) for base in bases]
    if not bases:
      # Old style class.
      bases = [self.convert.oldstyleclass_type.to_variable(self.root_cfg_node)]
    for b in sum((b.data for b in bases), []):
      if isinstance(b, abstract.ParameterizedClass) and any(
          self.annotations_util.get_type_parameters(v)
          for v in b.type_parameters.values()):
        self.errorlog.not_supported_yet(self.frames, "creating generic classes")
    if isinstance(class_dict, abstract.Unsolvable):
      # An unsolvable appears here if the vm hit maximum depth and gave up on
      # analyzing the class we're now building.
      var = self.convert.create_new_unsolvable(node)
    else:
      if cls_var is None:
        cls_var = class_dict.members.get("__metaclass__")
      if cls_var and all(v.data.full_name == "__builtin__.type"
                         for v in cls_var.bindings):
        cls_var = None
      try:
        val = abstract.InterpreterClass(
            name,
            bases,
            class_dict.pyval,
            cls_var,
            self)
      except mro.MROError as e:
        self.errorlog.mro_error(self.frames, name, e.mro_seqs)
        var = self.convert.create_new_unsolvable(node)
      else:
        var = self.program.NewVariable()
        var.AddBinding(val, class_dict_var.bindings, node)
        if not val.is_abstract:
          for member in sum((var.data for var in val.members.values()), []):
            if isinstance(member, abstract.Function) and member.is_abstract:
              self.errorlog.ignored_abstractmethod(
                  self.frames, val.name, member.name)
    return var

  def _make_function(self, name, node, code, globs, defaults, kw_defaults,
                     closure=None, annotations=None, late_annotations=None):
    """Create a function or closure given the arguments."""
    if closure:
      closure = tuple(c for c in abstract.get_atomic_python_constant(closure))
      log.info("closure: %r", closure)
    if not name:
      if abstract.get_atomic_python_constant(code).co_name:
        name = abstract.get_atomic_python_constant(code).co_name
      else:
        name = "<lambda>"
    val = abstract.InterpreterFunction.make_function(
        name, code=abstract.get_atomic_python_constant(code),
        f_locals=self.frame.f_locals, f_globals=globs,
        defaults=defaults, kw_defaults=kw_defaults,
        closure=closure, annotations=annotations,
        late_annotations=late_annotations, vm=self)
    # TODO(ampere): What else needs to be an origin in this case? Probably stuff
    # in closure.
    var = self.program.NewVariable()
    var.AddBinding(val, code.bindings, node)
    if late_annotations:
      self.functions_with_late_annotations.append(val)
    else:
      val.signature.check_type_parameter_count(self.frames)
    return var

  def make_frame(self, node, code, callargs=None,
                 f_globals=None, f_locals=None, closure=None, new_locals=None):
    """Create a new frame object, using the given args, globals and locals."""
    if any(code is f.f_code for f in self.frames):
      log.info("Detected recursion in %s", code.co_name or code.co_filename)
      raise RecursionException()

    log.info("make_frame: callargs=%s, f_globals=[%s@%x], f_locals=[%s@%x]",
             self.repper(callargs),
             type(f_globals).__name__, id(f_globals),
             type(f_locals).__name__, id(f_locals))
    if f_globals is not None:
      f_globals = f_globals
      assert f_locals
    else:
      assert not self.frames
      assert f_locals is None
      # TODO(ampere): __name__, __doc__, __package__ below are not correct
      f_globals = f_locals = self.convert_locals_or_globals({
          "__builtins__": self.loader.builtins,
          "__name__": "__main__",
          "__file__": code.co_filename,
          "__doc__": None,
          "__package__": None,
      })

    # Implement NEWLOCALS flag. See Objects/frameobject.c in CPython.
    # (Also allow to override this with a parameter, Python 3 doesn't always set
    #  it to the right value, e.g. for class-level code.)
    if code.co_flags & loadmarshal.CodeType.CO_NEWLOCALS or new_locals:
      f_locals = self.convert_locals_or_globals({}, "locals")

    return frame_state.Frame(node, self, code, f_globals, f_locals,
                             self.frame, callargs or {}, closure)

  def simple_stack(self, opcode=None):
    """Get a stack of simple frames.

    Args:
      opcode: Optionally, an opcode to create a stack for.

    Returns:
      If an opcode is provided, a stack with a single frame at that opcode.
      Otherwise, the VM's current stack converted to simple frames.
    """
    if opcode is not None:
      return [frame_state.SimpleFrame(opcode)]
    else:
      return [frame_state.SimpleFrame(frame.current_opcode)
              for frame in self.frames]

  def push_abstract_exception(self, state):
    tb = self.convert.build_list(state.node, [])
    value = self.convert.create_new_unknown(state.node)
    exctype = self.convert.create_new_unknown(state.node)
    return state.push(tb, value, exctype)

  def resume_frame(self, node, frame):
    frame.f_back = self.frame
    log.info("resume_frame: %r", frame)
    node, val = self.run_frame(frame, node)
    frame.f_back = None
    return node, val

  def compile_src(self, src, filename=None, mode="exec"):
    code = pyc.compile_src(
        src, python_version=self.python_version,
        python_exe=self.options.python_exe,
        filename=filename, mode=mode)
    return blocks.process_code(code)

  def run_bytecode(self, node, code, f_globals=None, f_locals=None):
    frame = self.make_frame(node, code, f_globals=f_globals, f_locals=f_locals)
    node, return_var = self.run_frame(frame, node)
    return node, frame.f_globals, frame.f_locals, return_var

  def preload_builtins(self, node):
    """Parse __builtin__.py and return the definitions as a globals dict."""
    if self.options.pybuiltins_filename:
      with open(self.options.pybuiltins_filename, "rb") as fi:
        src = fi.read()
    else:
      src = builtins.GetBuiltinsCode(self.python_version)
    builtins_code = self.compile_src(
        src, filename=self.options.pybuiltins_filename or "__builtin__.py")
    old = self.reading_builtins
    self.reading_builtins = True
    try:
      node, f_globals, f_locals, _ = self.run_bytecode(node, builtins_code)
    finally:
      self.reading_builtins = old
    assert not self.frames
    # TODO(kramm): pytype doesn't support namespacing of the currently parsed
    # module, so add the module name manually.
    for name, definition in f_globals.members.items():
      for d in definition.data:
        d.module = "__builtin__"
      self.trace_module_member(None, name, definition)
    return node, f_globals, f_locals

  def run_program(self, src, filename, maximum_depth, run_builtins):
    """Run the code and return the CFG nodes.

    This function loads in the builtins and puts them ahead of `code`,
    so all the builtins are available when processing `code`.

    Args:
      src: The program source code.
      filename: The filename the source is from.
      maximum_depth: Maximum depth to follow call chains.
      run_builtins: Whether to preload the native Python builtins.
    Returns:
      A tuple (CFGNode, set) containing the last CFGNode of the program as
        well as all the top-level names defined by it.
    """
    director = directors.Director(
        src, self.errorlog, filename, self.options.disable)
    # This modifies the errorlog passed to the constructor.  Kind of ugly,
    # but there isn't a better way to wire both pieces together.
    self.errorlog.set_error_filter(director.should_report_error)
    self.director = director
    self.filename = filename

    self.maximum_depth = sys.maxint if maximum_depth is None else maximum_depth
    node = self.root_cfg_node.ConnectNew("builtins")
    if run_builtins:
      node, f_globals, f_locals = self.preload_builtins(node)
    else:
      node, f_globals, f_locals = node, None, None

    code = self.compile_src(src, filename=filename)
    visitor = _FindIgnoredTypeComments(self.director.type_comments)
    pyc.visit(code, visitor)
    for line in visitor.ignored_lines():
      self.errorlog.ignored_type_comment(
          self.filename, line, self.director.type_comments[line][1])

    node = node.ConnectNew("init")
    node, f_globals, _, _ = self.run_bytecode(node, code, f_globals, f_locals)
    logging.info("Done running bytecode, postprocessing globals")
    for func in self.functions_with_late_annotations:
      self.annotations_util.eval_late_annotations(node, func, f_globals)
    for name, annot in f_globals.late_annotations.items():
      attr = self.annotations_util.init_annotation(
          annot.expr, annot.name, annot.stack, node, f_globals)
      self.attribute_handler.set_attribute(node, f_globals, name, attr)
      del f_globals.late_annotations[name]
    assert not self.frames, "Frames left over!"
    log.info("Final node: <%d>%s", node.id, node.name)
    return node, f_globals.members

  def _base(self, cls):
    if isinstance(cls, abstract.ParameterizedClass):
      return cls.base_cls
    return cls

  def _yield_subclass_superclass_pairs(self, subcls_var, supercls_var):
    """Get (subcls, supercls) pairs of abstract.AtomicAbstractValue objects."""
    if subcls_var and supercls_var:
      for subcls in subcls_var.data:
        for supercls in supercls_var.data:
          if supercls in subcls.mro:
            yield self._base(subcls), self._base(supercls)

  def _overrides(self, node, subcls_var, supercls_var, attr):
    """Check whether subcls_var overrides or newly defines the given attribute.

    Args:
      node: The current node.
      subcls_var: A variable of a potential subclass.
      supercls_var: A variable of a potential superclass.
      attr: An attribute name.

    Returns:
      True if subcls_var is a subclass of supercls_var and overrides or newly
      defines the attribute. False otherwise.
    """
    for subcls, supercls in self._yield_subclass_superclass_pairs(
        subcls_var, supercls_var):
      for cls in subcls.mro:
        if cls == supercls:
          break
        if cls.is_lazy:
          cls.load_lazy_attribute(attr)
        if attr in cls.members and cls.members[attr].Bindings(node):
          return True
    return False

  def _call_binop_on_bindings(self, node, name, xval, yval):
    """Call a binary operator on two cfg.Binding objects."""
    rname = self.reverse_operator_name(name)
    if rname and isinstance(xval.data, abstract.AMBIGUOUS_OR_EMPTY):
      # If the reverse operator is possible and x is ambiguous, then we have no
      # way of determining whether __{op} or __r{op}__ is called.  Technically,
      # the result is also unknown if y is ambiguous, but it is almost always
      # reasonable to assume that, e.g., "hello " + y is a string, even though
      # y could define __radd__.
      return node, self.program.NewVariable(
          [self.convert.unsolvable], {xval, yval}, node)
    options = [(xval, yval, name)]
    if rname:
      options.append((yval, xval, rname))
      if self._overrides(node, yval.data.cls, xval.data.cls, rname):
        # If y is a subclass of x and defines its own reverse operator, then we
        # need to try y.__r{op}__ before x.__{op}__.
        options.reverse()
    error = None
    for left_val, right_val, attr_name in options:
      node, attr_var = self.attribute_handler.get_attribute_generic(
          node, left_val.data, attr_name, left_val)
      if attr_var and attr_var.bindings:
        args = abstract.FunctionArgs(posargs=(right_val.AssignToNewVariable(),))
        try:
          return self.call_function(
              node, attr_var, args, fallback_to_unsolvable=False)
        except (abstract.DictKeyMissing, abstract.FailedFunctionCall) as e:
          # It's possible that this call failed because the function returned
          # NotImplemented.  See, e.g.,
          # test_operators.ReverseTest.check_reverse(), in which 1 {op} Bar()
          # ends up using Bar.__r{op}__. Thus, we need to save the error and
          # try the other operator.
          if e > error:
            error = e
    if error:
      raise error  # pylint: disable=raising-bad-type
    else:
      return node, None

  def call_binary_operator(self, state, name, x, y, report_errors=False):
    """Map a binary operator to "magic methods" (__add__ etc.)."""
    results = []
    log.debug("Calling binary operator %s", name)
    x_bindings = x.Bindings(state.node)
    y_bindings = y.Bindings(state.node)
    nodes = []
    error = None
    for xval in x_bindings:
      for yval in y_bindings:
        try:
          node, ret = self._call_binop_on_bindings(state.node, name, xval, yval)
        except (abstract.DictKeyMissing, abstract.FailedFunctionCall) as e:
          if e > error:
            error = e
        else:
          if ret:
            nodes.append(node)
            results.append(ret)
    if nodes:
      state = state.change_cfg_node(self.join_cfg_nodes(nodes))
    result = self.join_variables(state.node, results)
    log.debug("Result: %r %r", result, result.data)
    if not result.bindings and report_errors:
      if self._is_only_none(state.node, x):
        self.errorlog.none_attr(self.frames, name)
      elif error is None:
        self.errorlog.unsupported_operands(self.frames, name, x, y)
      elif isinstance(error, abstract.DictKeyMissing):
        self.errorlog.key_error(self.frames, error.name)
      else:
        self.errorlog.invalid_function_call(self.frames, error)
      result.AddBinding(self.convert.unsolvable, [], state.node)
    return state, result

  def call_inplace_operator(self, state, iname, x, y):
    """Try to call a method like __iadd__, possibly fall back to __add__."""
    state, attr = self.load_attr_noerror(state, x, iname)
    if attr is None:
      log.info("No inplace operator %s on %r", iname, x)
      name = iname.replace("i", "", 1)  # __iadd__ -> __add__ etc.
      state, ret = self.call_binary_operator(
          state, name, x, y, report_errors=True)
    else:
      # TODO(kramm): If x is a Variable with distinct types, both __add__
      # and __iadd__ might happen.
      try:
        state, ret = self.call_function_with_state(state, attr, (y,),
                                                   fallback_to_unsolvable=False)
      except abstract.FailedFunctionCall as e:
        self.errorlog.invalid_function_call(self.frames, e)
        ret = self.convert.create_new_unsolvable(state.node)
    return state, ret

  def binary_operator(self, state, name, report_errors=True):
    state, (x, y) = state.popn(2)
    state, ret = self.call_binary_operator(
        state, name, x, y, report_errors=report_errors)
    return state.push(ret)

  def inplace_operator(self, state, name):
    state, (x, y) = state.popn(2)
    state, ret = self.call_inplace_operator(state, name, x, y)
    return state.push(ret)

  def trace_module_member(self, *args):
    """Fired whenever a member of a module is converted."""
    return NotImplemented

  def trace_unknown(self, *args):
    """Fired whenever we create a variable containing 'Unknown'."""
    return NotImplemented

  def trace_call(self, *args):
    """Fired whenever we call a builtin using unknown parameters."""
    return NotImplemented

  def trace_functiondef(self, *args):
    return NotImplemented

  def trace_namedtuple(self, *args):
    return NotImplemented

  def call_init(self, node, unused_instance):
    # This dummy implementation is overwritten in infer.py.
    return node

  def call_function_with_state(self, state, funcu, posargs, namedargs=None,
                               starargs=None, starstarargs=None,
                               fallback_to_unsolvable=True):
    assert starargs is None or isinstance(starargs, typegraph.Variable)
    assert starstarargs is None or isinstance(starstarargs, typegraph.Variable)
    node, ret = self.call_function(state.node, funcu, abstract.FunctionArgs(
        posargs=posargs, namedargs=namedargs, starargs=starargs,
        starstarargs=starstarargs), fallback_to_unsolvable)
    return state.change_cfg_node(node), ret

  def _call_with_fake_args(self, node, funcu):
    """Attempt to call the given function with made-up arguments."""
    return node, self.convert.create_new_unsolvable(node)

  def call_function(self, node, funcu, args, fallback_to_unsolvable=True):
    """Call a function.

    Args:
      node: The current CFG node.
      funcu: A variable of the possible functions to call.
      args: The arguments to pass. See abstract.FunctionArgs.
      fallback_to_unsolvable: If the function call fails, create an unknown.
    Returns:
      A tuple (CFGNode, Variable). The Variable is the return value.
    Raises:
      DictKeyMissing: if we retrieved a nonexistent key from a dict and
        fallback_to_unsolvable is False.
      FailedFunctionCall: if the call fails and fallback_to_unsolvable is False.
    """
    assert funcu.bindings
    result = self.program.NewVariable()
    nodes = []
    error = None
    for funcv in funcu.bindings:
      func = funcv.data
      assert isinstance(func, abstract.AtomicAbstractValue), type(func)
      try:
        new_node, one_result = func.call(node, funcv, args)
      except (abstract.DictKeyMissing, abstract.FailedFunctionCall) as e:
        if e > error:
          error = e
      else:
        result.PasteVariable(one_result, new_node, {funcv})
        nodes.append(new_node)
    if nodes:
      node = self.join_cfg_nodes(nodes)
      if not result.bindings:
        result.AddBinding(self.convert.unsolvable, [], node)
      return node, result
    else:
      if fallback_to_unsolvable:
        if isinstance(error, abstract.DictKeyMissing):
          self.errorlog.key_error(self.frames, error.name)
        else:
          self.errorlog.invalid_function_call(self.frames, error)
          # If the function failed with a FailedFunctionCall exception, try
          # calling it again with fake arguments. This allows for calls to
          # __init__ to always succeed, ensuring pytype has a full view of the
          # class and its attributes.
          # If the call still fails, _call_with_fake_args will return
          # abstract.Unsolvable.
          if all(func.name == "__init__" for func in funcu.data):
            return self._call_with_fake_args(node, funcu)
        return node, self.convert.create_new_unsolvable(node)
      else:
        # We were called by something that does its own error handling.
        raise error  # pylint: disable=raising-bad-type

  def call_function_from_stack(self, state, num, starargs, starstarargs):
    """Pop arguments for a function and call it."""
    num_kw, num_pos = divmod(num, 256)

    # TODO(kramm): Can we omit creating this Dict if num_kw=0?
    namedargs = abstract.Dict(self)
    for _ in range(num_kw):
      state, (key, val) = state.popn(2)
      namedargs.setitem_slot(state.node, key, val)
    state, posargs = state.popn(num_pos)

    state, func = state.pop()
    state, ret = self.call_function_with_state(
        state, func, posargs, namedargs, starargs, starstarargs)
    state = state.push(ret)
    return state

  def get_globals_dict(self):
    """Get a real python dict of the globals."""
    return self.frame.f_globals

  def load_from(self, state, store, name):
    """Load an item out of locals, globals, or builtins."""
    assert isinstance(store, abstract.SimpleAbstractValue)
    assert store.is_lazy
    if name in store.late_annotations:
      # Unresolved late annotation. See attribute.py:get_attribute
      return state, self.convert.unsolvable.to_variable(state.node)
    store.load_lazy_attribute(name)
    bindings = store.members[name].Bindings(state.node)
    if not bindings:
      raise KeyError(name)
    return state, self.join_bindings(state.node, bindings)

  def load_local(self, state, name):
    """Called when a local is loaded onto the stack.

    Uses the name to retrieve the value from the current locals().

    Args:
      state: The current VM state.
      name: Name of the local

    Returns:
      The value (typegraph.Variable)
    """
    return self.load_from(state, self.frame.f_locals, name)

  def load_global(self, state, name):
    return self.load_from(state, self.frame.f_globals, name)

  def load_special_builtin(self, name):
    if name == "__any_object__":
      # For type_inferencer/tests/test_pgms/*.py, must be a new object
      # each time.
      return abstract.Unknown(self)
    else:
      return self.special_builtins.get(name)

  def load_builtin(self, state, name):
    if name == "__undefined__":
      # For values that don't exist. (Unlike None, which is a valid object)
      return state, self.convert.empty.to_variable(self.root_cfg_node)
    special = self.load_special_builtin(name)
    if special:
      return state, special.to_variable(state.node)
    else:
      return self.load_from(state, self.frame.f_builtins, name)

  def store_local(self, state, name, value):
    """Called when a local is written."""
    node = self.attribute_handler.set_attribute(
        state.node, self.frame.f_locals, name, value)
    return state.change_cfg_node(node)

  def store_global(self, state, name, value):
    """Same as store_local except for globals."""
    node = self.attribute_handler.set_attribute(
        state.node, self.frame.f_globals, name, value)
    return state.change_cfg_node(node)

  def del_local(self, name):
    """Called when a local is deleted."""
    # TODO(ampere): Implement locals removal or decide not to.
    log.warning("Local variable removal does not actually do "
                "anything in the abstract interpreter")

  def del_global(self, name):
    """Called when a global is deleted."""
    log.warning("Global variable removal does not actually do "
                "anything in the abstract interpreter")

  def _retrieve_attr(self, node, obj, attr):
    """Load an attribute from an object."""
    assert isinstance(obj, typegraph.Variable), obj
    # Resolve the value independently for each value of obj
    result = self.program.NewVariable()
    log.debug("getting attr %s from %r", attr, obj)
    nodes = []
    values_without_attribute = []
    for val in obj.Bindings(node):
      node2, attr_var = self.attribute_handler.get_attribute_generic(
          node, val.data, attr, val)
      if attr_var is None or not attr_var.bindings:
        log.debug("No %s on %s", attr, val.data.__class__)
        values_without_attribute.append(val)
        continue
      log.debug("got choice for attr %s from %r of %r (0x%x): %r", attr, obj,
                val.data, id(val.data), attr_var)
      result.PasteVariable(attr_var, node2)
      nodes.append(node2)
    if nodes:
      return self.join_cfg_nodes(nodes), result, values_without_attribute
    else:
      return node, None, values_without_attribute

  def _data_is_none(self, x):
    assert isinstance(x, abstract.AtomicAbstractValue)
    return x.cls and x.cls.data == [self.convert.none_type]

  def _is_none(self, node, binding):
    """Returns true if binding is None or unreachable."""
    return (self._data_is_none(binding.data) or
            not node.HasCombination([binding]))

  def _has_at_least_one_none(self, node, obj):
    return any(self._data_is_none(b.data) and node.HasCombination([b])
               for b in obj.bindings)

  def _is_only_none(self, node, obj):
    return (all(self._is_none(node, b) for b in obj.bindings) and
            self._has_at_least_one_none(node, obj))

  def _delete_item(self, state, obj, arg):
    state, f = self.load_attr(state, obj, "__delitem__")
    state, _ = self.call_function_with_state(state, f, (arg,))
    return state

  def load_attr(self, state, obj, attr):
    """Try loading an attribute, and report errors."""
    node, result, errors = self._retrieve_attr(state.node, obj, attr)
    if errors and obj.bindings and self._is_only_none(state.node, obj):
      self.errorlog.none_attr(self.frames, attr)
    else:
      for error in errors:
        if not self._is_none(state.node, error):
          self.errorlog.attribute_or_module_error(
              self.frames, error.AssignToNewVariable(self.root_cfg_node), attr)
    if result is None:
      result = self.convert.create_new_unsolvable(node)
    return state.change_cfg_node(node), result

  def load_attr_noerror(self, state, obj, attr):
    """Try loading an attribute, ignore errors."""
    node, result, _ = self._retrieve_attr(state.node, obj, attr)
    return state.change_cfg_node(node), result

  def store_attr(self, state, obj, attr, value):
    """Set an attribute on an object."""
    assert isinstance(obj, typegraph.Variable)
    assert isinstance(attr, str)
    if not obj.bindings:
      log.info("Ignoring setattr on %r", obj)
      return state
    nodes = []
    for val in obj.bindings:
      # TODO(kramm): Check whether val.data is a descriptor (i.e. has "__set__")
      nodes.append(self.attribute_handler.set_attribute(
          state.node, val.data, attr, value))
    return state.change_cfg_node(self.join_cfg_nodes(nodes))

  def del_attr(self, state, obj, attr):
    """Delete an attribute."""
    # TODO(kramm): Store abstract.Nothing
    log.warning("Attribute removal does not actually do "
                "anything in the abstract interpreter")
    return state

  def push_last_exception(self, state):
    log.info("Pushing exception %r", state.exception)
    exctype, value, tb = state.exception
    return state.push(tb, value, exctype)

  def del_subscr(self, state, obj, subscr):
    return self._delete_item(state, obj, subscr)

  def pop_varargs(self, state):
    """Retrieve a varargs tuple from the stack. Used by call_function."""
    return state.pop()

  def pop_kwargs(self, state):
    """Retrieve a kwargs dictionary from the stack. Used by call_function."""
    return state.pop()

  def convert_locals_or_globals(self, d, name="globals"):
    return abstract.LazyConcreteDict(name, d, self)

  def import_module(self, name, full_name, level):
    try:
      module = self._import_module(name, level)
    except (parser.ParseError, load_pytd.BadDependencyError,
            visitors.ContainerError, visitors.SymbolLookupError) as e:
      self.errorlog.pyi_error(self.frames, full_name, e)
      module = self.convert.unsolvable
    return module

  # TODO(kramm): memoize
  def _import_module(self, name, level):
    """Import the module and return the module object.

    Args:
      name: Name of the module. E.g. "sys".
      level: Specifies whether to use absolute or relative imports.
        -1: (Python <= 3.1) "Normal" import. Try both relative and absolute.
         0: Absolute import.
         1: "from . import abc"
         2: "from .. import abc"
         etc.
    Returns:
      An instance of abstract.Module or None if we couldn't find the module.
    """
    if name:
      if level <= 0:
        assert level in [-1, 0]
        if name in overlays:
          if name not in self.loaded_overlays:
            self.loaded_overlays[name] = overlays[name](self)
          return self.loaded_overlays[name]
        elif level == -1 and self.loader.base_module:
          # Python 2 tries relative imports first.
          ast = (self.loader.import_relative_name(name) or
                 self.loader.import_name(name))
        else:
          ast = self.loader.import_name(name)
      else:
        # "from .x import *"
        base = self.loader.import_relative(level)
        if base is None:
          return None
        ast = self.loader.import_name(base.name + "." + name)
    else:
      assert level > 0
      ast = self.loader.import_relative(level)
    if ast:
      return self.convert.constant_to_value(
          ast, subst={}, node=self.root_cfg_node)
    else:
      return None

  def print_item(self, item, to=None):
    # We don't need do anything here, since Python's print function accepts
    # any type. (We could exercise the __str__ method on item - but every
    # object has a __str__, so we wouldn't learn anything from that.)
    pass

  def print_newline(self, to=None):
    pass

  def unary_operator(self, state, name):
    state, x = state.pop()
    state, method = self.load_attr(state, x, name)  # E.g. __not__
    state, result = self.call_function_with_state(state, method, ())
    state = state.push(result)
    return state

  def expand_bool_result(self, node, left, right, name, maybe_predicate):
    result = self.program.NewVariable()
    for x in left.Bindings(node):
      for y in right.Bindings(node):
        pyval = maybe_predicate(x.data, y.data)
        result.AddBinding(self.convert.bool_values[pyval],
                          source_set=(x, y), where=node)

    return result

  def _get_iter(self, state, seq, report_errors=True):
    """Get an iterator from a sequence."""
    state, func = self.load_attr_noerror(state, seq, "__iter__")
    if func:
      # Call __iter__()
      state, itr = self.call_function_with_state(state, func, ())
    else:
      node, func, missing = self._retrieve_attr(state.node, seq, "__getitem__")
      state = state.change_cfg_node(node)
      if func:
        # TODO(dbaum): Consider delaying the call to __getitem__ until
        # the iterator's next() is called.  That would more closely match
        # actual execution at the cost of making the code and Iterator class
        # a little more complicated.

        # Call __getitem__(int).
        state, item = self.call_function_with_state(
            state, func, (self.convert.build_int(state.node),))
        # Create a new iterator from the returned value.
        itr = abstract.Iterator(self, item).to_variable(state.node)
      else:
        # Cannot iterate this object.
        if report_errors:
          for m in missing:
            if state.node.HasCombination([m]):
              if self._data_is_none(m.data):
                self.errorlog.none_attr(self.frames, "__iter__")
              else:
                self.errorlog.attribute_error(self.frames, seq, "__iter__")
          itr = self.convert.create_new_unsolvable(state.node)
        else:
          itr = None
    return state, itr

  def byte_UNARY_NOT(self, state, op):
    """Implement the UNARY_NOT bytecode."""
    state, var = state.pop()
    bindings = var.Bindings(state.node)
    true_bindings = [b for b in bindings if b.data.compatible_with(True)]
    false_bindings = [b for b in bindings if b.data.compatible_with(False)]
    if len(true_bindings) == len(false_bindings) == len(bindings):
      # No useful information from bindings, use a generic bool value.
      # This is merely an optimization rather than building separate True/False
      # values each with the same bindings as var.
      result = self.convert.build_bool(state.node)
    else:
      # Build a result with True/False values, each bound to appropriate
      # bindings.  Note that bindings that are True get attached to a result
      # that is False and vice versa because this is a NOT operation.
      result = self.program.NewVariable()
      for b in true_bindings:
        result.AddBinding(self.convert.bool_values[False],
                          source_set=(b,), where=state.node)
      for b in false_bindings:
        result.AddBinding(self.convert.bool_values[True],
                          source_set=(b,), where=state.node)
    state = state.push(result)
    return state

  def byte_UNARY_CONVERT(self, state, op):
    return self.unary_operator(state, "__repr__")

  def byte_UNARY_NEGATIVE(self, state, op):
    return self.unary_operator(state, "__neg__")

  def byte_UNARY_POSITIVE(self, state, op):
    return self.unary_operator(state, "__pos__")

  def byte_UNARY_INVERT(self, state, op):
    return self.unary_operator(state, "__invert__")

  def byte_BINARY_MATRIX_MULTIPLY(self, state, op):
    return self.binary_operator(state, "__matmul__")

  def byte_BINARY_ADD(self, state, op):
    return self.binary_operator(state, "__add__")

  def byte_BINARY_SUBTRACT(self, state, op):
    return self.binary_operator(state, "__sub__")

  def byte_BINARY_DIVIDE(self, state, op):
    return self.binary_operator(state, "__div__")

  def byte_BINARY_MULTIPLY(self, state, op):
    return self.binary_operator(state, "__mul__")

  def byte_BINARY_MODULO(self, state, op):
    return self.binary_operator(state, "__mod__")

  def byte_BINARY_LSHIFT(self, state, op):
    return self.binary_operator(state, "__lshift__")

  def byte_BINARY_RSHIFT(self, state, op):
    return self.binary_operator(state, "__rshift__")

  def byte_BINARY_AND(self, state, op):
    return self.binary_operator(state, "__and__")

  def byte_BINARY_XOR(self, state, op):
    return self.binary_operator(state, "__xor__")

  def byte_BINARY_OR(self, state, op):
    return self.binary_operator(state, "__or__")

  def byte_BINARY_FLOOR_DIVIDE(self, state, op):
    return self.binary_operator(state, "__floordiv__")

  def byte_BINARY_TRUE_DIVIDE(self, state, op):
    return self.binary_operator(state, "__truediv__")

  def byte_BINARY_POWER(self, state, op):
    return self.binary_operator(state, "__pow__")

  def byte_BINARY_SUBSCR(self, state, op):
    return self.binary_operator(state, "__getitem__")

  def byte_INPLACE_MATRIX_MULTIPLY(self, state, op):
    return self.inplace_operator(state, "__imatmul__")

  def byte_INPLACE_ADD(self, state, op):
    return self.inplace_operator(state, "__iadd__")

  def byte_INPLACE_SUBTRACT(self, state, op):
    return self.inplace_operator(state, "__isub__")

  def byte_INPLACE_MULTIPLY(self, state, op):
    return self.inplace_operator(state, "__imul__")

  def byte_INPLACE_DIVIDE(self, state, op):
    return self.inplace_operator(state, "__idiv__")

  def byte_INPLACE_MODULO(self, state, op):
    return self.inplace_operator(state, "__imod__")

  def byte_INPLACE_POWER(self, state, op):
    return self.inplace_operator(state, "__ipow__")

  def byte_INPLACE_LSHIFT(self, state, op):
    return self.inplace_operator(state, "__ilshift__")

  def byte_INPLACE_RSHIFT(self, state, op):
    return self.inplace_operator(state, "__irshift__")

  def byte_INPLACE_AND(self, state, op):
    return self.inplace_operator(state, "__iand__")

  def byte_INPLACE_XOR(self, state, op):
    return self.inplace_operator(state, "__ixor__")

  def byte_INPLACE_OR(self, state, op):
    return self.inplace_operator(state, "__ior__")

  def byte_INPLACE_FLOOR_DIVIDE(self, state, op):
    return self.inplace_operator(state, "__ifloordiv__")

  def byte_INPLACE_TRUE_DIVIDE(self, state, op):
    return self.inplace_operator(state, "__itruediv__")

  def byte_LOAD_CONST(self, state, op):
    raw_const = self.frame.f_code.co_consts[op.arg]
    const = self.convert.constant_to_var(raw_const, node=state.node)
    return state.push(const)

  def byte_POP_TOP(self, state, op):
    return state.pop_and_discard()

  def byte_DUP_TOP(self, state, op):
    return state.push(state.top())

  def byte_DUP_TOPX(self, state, op):
    state, items = state.popn(op.arg)
    state = state.push(*items)
    state = state.push(*items)
    return state

  def byte_DUP_TOP_TWO(self, state, op):
    # Py3 only
    state, (a, b) = state.popn(2)
    return state.push(a, b, a, b)

  def byte_ROT_TWO(self, state, op):
    state, (a, b) = state.popn(2)
    return state.push(b, a)

  def byte_ROT_THREE(self, state, op):
    state, (a, b, c) = state.popn(3)
    return state.push(c, a, b)

  def byte_ROT_FOUR(self, state, op):
    state, (a, b, c, d) = state.popn(4)
    return state.push(d, a, b, c)

  def _is_private(self, name):
    return name.startswith("_") and not name.startswith("__")

  def byte_LOAD_NAME(self, state, op):
    """Load a name. Can be a local, global, or builtin."""
    name = self.frame.f_code.co_names[op.arg]
    try:
      state, val = self.load_local(state, name)
    except KeyError:
      try:
        state, val = self.load_global(state, name)
      except KeyError:
        try:
          if self._is_private(name):
            # Private names must be explicitly imported.
            raise KeyError()
          state, val = self.load_builtin(state, name)
        except KeyError:
          if self._is_private(name) or not self.has_unknown_wildcard_imports:
            self.errorlog.name_error(self.frames, name)
          return state.push(
              self.convert.create_new_unsolvable(state.node))
    return state.push(val)

  def byte_STORE_NAME(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    state, value = state.pop()
    value = self.annotations_util.apply_type_comment(state, op, name, value)
    state = self.store_local(state, name, value)
    return state.forward_cfg_node()

  def byte_DELETE_NAME(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    self.del_local(name)
    return state

  def byte_LOAD_FAST(self, state, op):
    """Load a local. Unlike LOAD_NAME, it doesn't fall back to globals."""
    name = self.frame.f_code.co_varnames[op.arg]
    try:
      state, val = self.load_local(state, name)
    except KeyError:
      self.errorlog.name_error(self.frames, name)
      val = self.convert.create_new_unsolvable(state.node)
    return state.push(val)

  def byte_STORE_FAST(self, state, op):
    name = self.frame.f_code.co_varnames[op.arg]
    state, value = state.pop()
    value = self.annotations_util.apply_type_comment(state, op, name, value)
    state = state.forward_cfg_node()
    state = self.store_local(state, name, value)
    state = state.forward_cfg_node()
    return state

  def byte_DELETE_FAST(self, state, op):
    name = self.frame.f_code.co_varnames[op.arg]
    self.del_local(name)
    return state

  def byte_LOAD_GLOBAL(self, state, op):
    """Load a global variable, or fall back to trying to load a builtin."""
    name = self.frame.f_code.co_names[op.arg]
    try:
      state, val = self.load_global(state, name)
    except KeyError:
      try:
        state, val = self.load_builtin(state, name)
      except KeyError:
        self.errorlog.name_error(self.frames, name)
        return state.push(self.convert.create_new_unsolvable(state.node))
    return state.push(val)

  def byte_STORE_GLOBAL(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    state, value = state.pop()
    value = self.annotations_util.apply_type_comment(state, op, name, value)
    state = self.store_global(state, name, value)
    return state

  def byte_DELETE_GLOBAL(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    self.del_global(name)
    return state

  def byte_LOAD_CLOSURE(self, state, op):
    """Used to generate the 'closure' tuple for MAKE_CLOSURE.

    Each entry in that tuple is typically retrieved using LOAD_CLOSURE.

    Args:
      state: The current VM state.
      op: The opcode. op.arg is the index of a "cell variable": This corresponds
        to an entry in co_cellvars or co_freevars and is a variable that's bound
        into a closure.
    Returns:
      A new state.
    """
    return state.push(self.frame.cells[op.arg])

  def byte_LOAD_DEREF(self, state, op):
    """Retrieves a value out of a cell."""
    # Since we're working on typegraph.Variable, we don't need to dereference.
    return state.push(self.frame.cells[op.arg])

  def byte_STORE_DEREF(self, state, op):
    """Stores a value in a closure cell."""
    state, value = state.pop()
    assert isinstance(value, typegraph.Variable)
    self.frame.cells[op.arg].PasteVariable(value, state.node)
    return state

  def byte_DELETE_DEREF(self, state, op):
    # TODO(mdemello): Figure out what to do here.
    log.warning("DELETE_DEREF does not do anything.")
    return state

  def byte_LOAD_LOCALS(self, state, op):
    log.debug("Returning locals: %r", self.frame.f_locals)
    locals_dict = self.frame.f_locals.to_variable(self.root_cfg_node)
    return state.push(locals_dict)

  def _cmp_eq(self, state, x, y, eq=True):
    """Implementation of CMP_EQ/CMP_NE.

    Args:
      state: Initial FrameState.
      x: A variable of the lhs value.
      y: A variable of the rhs value.
      eq: True or False (indicates which value to return when x == y).

    Returns:
      A tuple of the new FrameState and the return variable.
    """
    ret = self.program.NewVariable()
    # A variable of the values without a special cmp_eq implementation. Needed
    # because overloaded __eq__ implementations do not necessarily return a
    # bool; see, e.g., test_overloaded in test_cmp.
    leftover = self.program.NewVariable()
    for b1 in x.bindings:
      for b2 in y.bindings:
        val = compare.cmp_eq(self, b1.data, b2.data)
        if val is None:
          leftover.AddBinding(b1.data, {b1}, state.node)
        else:
          ret.AddBinding(
              self.convert.bool_values[val is eq], {b1, b2}, state.node)
    if leftover.bindings:
      op = "__eq__" if eq else "__ne__"
      state, leftover_ret = self.call_binary_operator(state, op, leftover, y)
      ret.PasteVariable(leftover_ret, state.node)
    return state, ret

  def _coerce_to_bool(self, node, var, true_val=True):
    """Coerce the values in a variable to bools."""
    bool_var = self.program.NewVariable()
    for b in var.bindings:
      v = b.data
      if isinstance(v, abstract.PythonConstant) and isinstance(v.pyval, bool):
        const = v.pyval is true_val
      elif not v.compatible_with(True):
        const = False is true_val
      elif not v.compatible_with(False):
        const = True is true_val
      else:
        const = None
      bool_var.AddBinding(self.convert.bool_values[const], {b}, node)
    return bool_var

  def _cmp_in(self, state, x, y, true_val=True):
    """Implementation of CMP_IN/CMP_NOT_IN."""
    state, ret = self.call_binary_operator(state, "__contains__", y, x)
    if ret.bindings:
      ret = self._coerce_to_bool(state.node, ret, true_val=true_val)
    else:
      # For an object without a __contains__ method, cmp_in falls back to
      # checking x against the items produced by y's iterator.
      state, itr = self._get_iter(state, y, report_errors=False)
      if not itr:
        # y does not have any of __contains__, __iter__, and __getitem__.
        # (The last two are checked by _get_iter.)
        if self._is_only_none(state.node, y):
          self.errorlog.none_attr(self.frames, "__contains__")
        else:
          self.errorlog.unsupported_operands(self.frames, "__contains__", y, x)
      ret = self.convert.build_bool(state.node)
    return state, ret

  def byte_COMPARE_OP(self, state, op):
    """Pops and compares the top two stack values and pushes a boolean."""
    state, (x, y) = state.popn(2)
    # Explicit, redundant, switch statement, to make it easier to address the
    # behavior of individual compare operations:
    if op.arg == slots.CMP_LT:
      state, ret = self.call_binary_operator(state, "__lt__", x, y)
    elif op.arg == slots.CMP_LE:
      state, ret = self.call_binary_operator(state, "__le__", x, y)
    elif op.arg == slots.CMP_EQ:
      state, ret = self._cmp_eq(state, x, y)
    elif op.arg == slots.CMP_NE:
      state, ret = self._cmp_eq(state, x, y, eq=False)
    elif op.arg == slots.CMP_GT:
      state, ret = self.call_binary_operator(state, "__gt__", x, y)
    elif op.arg == slots.CMP_GE:
      state, ret = self.call_binary_operator(state, "__ge__", x, y)
    elif op.arg == slots.CMP_IS:
      ret = self.expand_bool_result(state.node, x, y,
                                    "is_cmp", frame_state.is_cmp)
    elif op.arg == slots.CMP_IS_NOT:
      ret = self.expand_bool_result(state.node, x, y,
                                    "is_not_cmp", frame_state.is_not_cmp)
    elif op.arg == slots.CMP_NOT_IN:
      state, ret = self._cmp_in(state, x, y, true_val=False)
    elif op.arg == slots.CMP_IN:
      state, ret = self._cmp_in(state, x, y)
    elif op.arg == slots.CMP_EXC_MATCH:
      ret = self.convert.build_bool(state.node)
    else:
      raise VirtualMachineError("Invalid argument to COMPARE_OP: %d", op.arg)
    if not ret.bindings and op.arg in slots.CMP_ALWAYS_SUPPORTED:
      # Some comparison operations are always supported.
      # (https://docs.python.org/2/library/stdtypes.html#comparisons)
      ret.AddBinding(
          self.convert.primitive_class_instances[bool], [], state.node)
    return state.push(ret)

  def byte_LOAD_ATTR(self, state, op):
    """Pop an object, and retrieve a named attribute from it."""
    name = self.frame.f_code.co_names[op.arg]
    state, obj = state.pop()
    log.debug("LOAD_ATTR: %r %r", obj, name)
    state, val = self.load_attr(state, obj, name)
    return state.push(val)

  def byte_STORE_ATTR(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    state, (val, obj) = state.popn(2)
    val = self.annotations_util.apply_type_comment(state, op, name, val)
    state = state.forward_cfg_node()
    state = self.store_attr(state, obj, name, val)
    state = state.forward_cfg_node()
    return state

  def byte_DELETE_ATTR(self, state, op):
    name = self.frame.f_code.co_names[op.arg]
    state, obj = state.pop()
    return self.del_attr(state, obj, name)

  def store_subscr(self, state, obj, key, val):
    state, f = self.load_attr(state, obj, "__setitem__")
    state, _ = self.call_function_with_state(state, f, (key, val))
    return state

  def byte_STORE_SUBSCR(self, state, op):
    state, (val, obj, subscr) = state.popn(3)
    state = state.forward_cfg_node()
    state = self.store_subscr(state, obj, subscr, val)
    return state

  def byte_DELETE_SUBSCR(self, state, op):
    state, (obj, subscr) = state.popn(2)
    return self.del_subscr(state, obj, subscr)

  def byte_BUILD_TUPLE(self, state, op):
    count = op.arg
    state, elts = state.popn(count)
    return state.push(self.convert.build_tuple(state.node, elts))

  def byte_BUILD_LIST(self, state, op):
    count = op.arg
    state, elts = state.popn(count)
    return state.push(self.convert.build_list(state.node, elts))

  def byte_BUILD_SET(self, state, op):
    count = op.arg
    state, elts = state.popn(count)
    return state.push(self.convert.build_set(state.node, elts))

  def byte_BUILD_MAP(self, state, op):
    """Build a dictionary."""
    the_map = self.convert.build_map(state.node)
    if self.python_version >= (3, 5):
      state, args = state.popn(2 * op.arg)
      for i in xrange(op.arg):
        key, val = args[2*i], args[2*i+1]
        state = self.store_subscr(state, the_map, key, val)
    else:
      # For python < 3.5 we build the map in STORE_MAP
      pass
    return state.push(the_map)

  def byte_STORE_MAP(self, state, op):
    state, (the_map, val, key) = state.popn(3)
    state = self.store_subscr(state, the_map, key, val)
    return state.push(the_map)

  def _get_literal_sequence(self, data):
    """Helper function for _unpack_sequence."""
    try:
      return self.convert.value_to_constant(data, tuple)
    except abstract.ConversionError:
      # Fall back to looking for a literal list and converting to a tuple
      try:
        return tuple(self.convert.value_to_constant(data, list))
      except abstract.ConversionError:
        if data.cls and len(data.cls.bindings) == 1:
          cls, = data.cls.data
          for base in cls.mro:
            if isinstance(base, abstract.TupleClass):
              # Found a subclass of a heterogenous tuple (usually a
              # typing.NamedTuple instance).
              new_data = abstract.merge_values(
                  base.instantiate(self.root_cfg_node).data, self)
              return self._get_literal_sequence(new_data)
        return None

  def _restructure_tuple(self, state, tup, pre, post):
    """Collapse the middle part of a tuple into a List variable."""
    before = tup[0:pre]
    if post > 0:
      after = tup[-post:]
      rest = tup[pre:-post]
    else:
      after = ()
      rest = tup[pre:]
    rest = self.convert.build_list(state.node, rest)
    return before + (rest,) + after

  def _unpack_sequence(self, state, n_before, n_after=-1):
    """Pops a tuple (or other iterable) and pushes it onto the VM's stack.

    Supports destructuring assignment with potentially a single list variable
    that slurps up the remaining elements:
    1. a, b, c = ...  # UNPACK_SEQUENCE
    2. a, *b, c = ... # UNPACK_EX

    Args:
      state: The current VM state
      n_before: Number of elements before the list (n_elements for case 1)
      n_after: Number of elements after the list (-1 for case 1)
    Returns:
      The new state.
    """
    assert n_after >= -1
    state, seq = state.pop()
    options = []
    nontuple_seq = self.program.NewVariable()
    has_slurp = n_after > -1
    count = n_before + n_after + 1
    for b in seq.bindings:
      tup = self._get_literal_sequence(b.data)
      if tup:
        # TODO(rechen): pytype error if the length is wrong?
        if has_slurp and len(tup) >= count:
          options.append(self._restructure_tuple(state, tup, n_before, n_after))
          continue
        elif len(tup) == count:
          options.append(tup)
          continue
      nontuple_seq.AddBinding(b.data, {b}, state.node)
    if nontuple_seq.bindings:
      state, itr = self._get_iter(state, nontuple_seq)
      state, f = self.load_attr(state, itr, "next")
      state, result = self.call_function_with_state(state, f, ())
      # For a non-literal iterable, next() should always return the same type T,
      # so we can iterate `count` times in both UNPACK_SEQUENCE and UNPACK_EX,
      # and assign the slurp variable type List[T].
      option = [result for _ in range(count)]
      if has_slurp:
        option[n_before] = self.convert.build_list_of_type(state.node, result)
      options.append(option)
    values = tuple(self.convert.build_content(value) for value in zip(*options))
    for value in reversed(values):
      if not value.bindings:
        # For something like
        #   for i, j in enumerate(()):
        #     print j
        # there are no bindings for j, so we have to add an empty binding
        # to avoid a name error on the print statement.
        value = self.convert.empty.to_variable(state.node)
      state = state.push(value)
    return state

  def byte_UNPACK_SEQUENCE(self, state, op):
    return self._unpack_sequence(state, op.arg)

  def byte_UNPACK_EX(self, state, op):
    n_before = op.arg & 0xff
    n_after = op.arg >> 8
    return self._unpack_sequence(state, n_before, n_after)

  def byte_BUILD_SLICE(self, state, op):
    if op.arg == 2:
      state, (x, y) = state.popn(2)
      return state.push(self.convert.build_slice(state.node, x, y))
    elif op.arg == 3:
      state, (x, y, z) = state.popn(3)
      return state.push(self.convert.build_slice(state.node, x, y, z))
    else:       # pragma: no cover
      raise VirtualMachineError("Strange BUILD_SLICE count: %r" % op.arg)

  def byte_LIST_APPEND(self, state, op):
    # Used by the compiler e.g. for [x for x in ...]
    count = op.arg
    state, val = state.pop()
    the_list = state.peek(count)
    state, f = self.load_attr(state, the_list, "append")
    state, _ = self.call_function_with_state(state, f, (val,))
    return state

  def byte_SET_ADD(self, state, op):
    # Used by the compiler e.g. for {x for x in ...}
    count = op.arg
    state, val = state.pop()
    the_set = state.peek(count)
    state, f = self.load_attr(state, the_set, "add")
    state, _ = self.call_function_with_state(state, f, (val,))
    return state

  def byte_MAP_ADD(self, state, op):
    # Used by the compiler e.g. for {x, y for x, y in ...}
    count = op.arg
    state, (val, key) = state.popn(2)
    the_map = state.peek(count)
    state, f = self.load_attr(state, the_map, "__setitem__")
    state, _ = self.call_function_with_state(state, f, (key, val))
    return state

  def byte_PRINT_EXPR(self, state, op):
    # Only used in the interactive interpreter, not in modules.
    return state.pop_and_discard()

  def byte_PRINT_ITEM(self, state, op):
    state, item = state.pop()
    self.print_item(item)
    return state

  def byte_PRINT_ITEM_TO(self, state, op):
    state, to = state.pop()
    state, item = state.pop()
    self.print_item(item, to)
    return state

  def byte_PRINT_NEWLINE(self, state, op):
    self.print_newline()
    return state

  def byte_PRINT_NEWLINE_TO(self, state, op):
    state, to = state.pop()
    self.print_newline(to)
    return state

  def _jump_if(self, state, op, pop=False, jump_if=False, or_pop=False):
    """Implementation of various _JUMP_IF bytecodes.

    Args:
      state: Initial FrameState.
      op: An opcode.
      pop: True if a value is popped off the stack regardless.
      jump_if: True or False (indicates which value will lead to a jump).
      or_pop: True if a value is popped off the stack only when the jump is
          not taken.
    Returns:
      The new FrameState.
    """
    assert not (pop and or_pop)
    # Determine the conditions.  Assume jump-if-true, then swap conditions
    # if necessary.
    if pop:
      state, value = state.pop()
    else:
      value = state.top()
    jump, normal = frame_state.split_conditions(
        state.node, value)
    if not jump_if:
      jump, normal = normal, jump
    # Jump.
    if jump is not frame_state.UNSATISFIABLE:
      if jump:
        assert jump.binding
        else_node = state.forward_cfg_node(jump.binding).forward_cfg_node()
      else:
        else_node = state.forward_cfg_node()
      self.store_jump(op.target, else_node)
    else:
      else_node = None
    # Don't jump.
    if or_pop:
      state = state.pop_and_discard()
    if normal is frame_state.UNSATISFIABLE:
      return state.set_why("unsatisfiable")
    elif not else_node and not normal:
      return state  # We didn't actually branch.
    else:
      return state.forward_cfg_node(normal.binding if normal else None)

  def byte_JUMP_IF_TRUE_OR_POP(self, state, op):
    return self._jump_if(state, op, jump_if=True, or_pop=True)

  def byte_JUMP_IF_FALSE_OR_POP(self, state, op):
    return self._jump_if(state, op, jump_if=False, or_pop=True)

  def byte_JUMP_IF_TRUE(self, state, op):  # Not in py2.7
    return self._jump_if(state, op, jump_if=True)

  def byte_JUMP_IF_FALSE(self, state, op):  # Not in py2.7
    return self._jump_if(state, op, jump_if=False)

  def byte_POP_JUMP_IF_TRUE(self, state, op):
    return self._jump_if(state, op, pop=True, jump_if=True)

  def byte_POP_JUMP_IF_FALSE(self, state, op):
    return self._jump_if(state, op, pop=True, jump_if=False)

  def byte_JUMP_FORWARD(self, state, op):
    self.store_jump(op.target, state.forward_cfg_node())
    return state

  def byte_JUMP_ABSOLUTE(self, state, op):
    self.store_jump(op.target, state.forward_cfg_node())
    return state

  def byte_SETUP_LOOP(self, state, op):
    return self.push_block(state, "loop", op, op.target)

  def byte_GET_ITER(self, state, op):
    """Get the iterator for an object."""
    state, seq = state.pop()
    state, itr = self._get_iter(state, seq)
    # Push the iterator onto the stack and return.
    return state.push(itr)

  def store_jump(self, target, state):
    self.frame.states[target] = state.merge_into(self.frame.states.get(target))

  def byte_FOR_ITER(self, state, op):
    self.store_jump(op.target, state.pop_and_discard())
    state, f = self.load_attr(state, state.top(), "next")
    state = state.push(f)
    return self.call_function_from_stack(state, 0, None, None)

  def _revert_state_to(self, state, name):
    while state.block_stack[-1].type != name:
      state, block = state.pop_block()
      while block.level < len(state.data_stack):
        state = state.pop_and_discard()
    return state

  def byte_BREAK_LOOP(self, state, op):
    new_state, block = self._revert_state_to(state, "loop").pop_block()
    while block.level < len(new_state.data_stack):
      new_state = new_state.pop_and_discard()
    self.store_jump(op.block_target, new_state)
    return state

  def byte_CONTINUE_LOOP(self, state, op):
    new_state = self._revert_state_to(state, "loop")
    self.store_jump(op.target, new_state)
    return state

  def byte_SETUP_EXCEPT(self, state, op):
    # Assume that it's possible to throw the exception at the first
    # instruction of the code:
    self.store_jump(op.target, self.push_abstract_exception(state))
    return self.push_block(state, "setup-except", op, op.target)

  def byte_SETUP_FINALLY(self, state, op):
    # Emulate finally by connecting the try to the finally block (with
    # empty reason/why/continuation):
    self.store_jump(op.target, state.push(self.convert.build_none(state.node)))
    return self.push_block(state, "finally", op, op.target)

  def byte_POP_BLOCK(self, state, op):
    state, _ = state.pop_block()
    return state

  def byte_RAISE_VARARGS_PY2(self, state, op):
    """Raise an exception (Python 2 version)."""
    # NOTE: the dis docs are completely wrong about the order of the
    # operands on the stack!
    argc = op.arg
    exctype = val = tb = None
    if argc == 0:
      if state.exception is None:
        raise exceptions.ByteCodeTypeError(
            "exceptions must be old-style classes "
            "or derived from BaseException, not NoneType")
      exctype, val, tb = state.exception
    elif argc == 1:
      state, exctype = state.pop()
    elif argc == 2:
      state, val = state.pop()
      state, exctype = state.pop()
    elif argc == 3:
      state, tb = state.pop()
      state, val = state.pop()
      state, exctype = state.pop()
    # There are a number of forms of "raise", normalize them somewhat.
    if isinstance(exctype, BaseException):
      val = exctype
      exctype = type(val)
    state = state.set_exception(exctype, val, tb)
    if tb:
      return state.set_why("reraise")
    else:
      return state.set_why("exception")

  def byte_RAISE_VARARGS_PY3(self, state, op):
    """Raise an exception (Python 3 version)."""
    argc = op.arg
    cause = exc = None
    if argc == 2:
      state, cause = state.pop()
      state, exc = state.pop()
    elif argc == 1:
      state, exc = state.pop()
    return self.do_raise(state, exc, cause)

  def byte_RAISE_VARARGS(self, state, op):
    if self.python_version[0] == 2:
      return self.byte_RAISE_VARARGS_PY2(state, op)
    else:
      return self.byte_RAISE_VARARGS_PY3(state, op)

  def byte_POP_EXCEPT(self, state, op):  # Python 3 only
    # We don't push the special except-handler block, so we don't need to
    # pop it, either.
    return state

  def byte_SETUP_WITH(self, state, op):
    """Starts a 'with' statement. Will push a block."""
    state, ctxmgr = state.pop()
    level = len(state.data_stack)
    state, exit_method = self.load_attr(state, ctxmgr, "__exit__")
    state = state.push(exit_method)
    state, enter = self.load_attr(state, ctxmgr, "__enter__")
    state, ctxmgr_obj = self.call_function_with_state(state, enter, ())
    if self.python_version[0] == 2:
      state = self.push_block(state, "with", op, op.target, level)
    else:
      assert self.python_version[0] == 3
      state = self.push_block(state, "finally", op, op.target, level)
    return state.push(ctxmgr_obj)

  def byte_WITH_CLEANUP(self, state, op):
    """Called at the end of a with block. Calls the exit handlers etc."""
    state, u = state.pop()  # pop 'None'
    state, exit_func = state.pop()
    state = state.push(self.convert.build_none(state.node))
    v = self.convert.build_none(state.node)
    w = self.convert.build_none(state.node)
    state, unused_suppress_exception = self.call_function_with_state(
        state, exit_func, (u, v, w))
    return state

  def _convert_kw_defaults(self, values):
    kw_defaults = {}
    for i in range(0, len(values), 2):
      key_var, value = values[i:i + 2]
      key = abstract.get_atomic_python_constant(key_var)
      kw_defaults[key] = value
    return kw_defaults

  def _get_extra_function_args(self, state, arg):
    """Get function annotations and defaults from the stack. (Python3.5-)."""
    if self.python_version[0] == 2:
      num_pos_defaults = arg & 0xffff
      num_kw_defaults = 0
    else:
      assert self.python_version[0] == 3
      num_pos_defaults = arg & 0xff
      num_kw_defaults = (arg >> 8) & 0xff
    state, raw_annotations = state.popn((arg >> 16) & 0x7fff)
    state, kw_defaults = state.popn(2 * num_kw_defaults)
    state, pos_defaults = state.popn(num_pos_defaults)
    free_vars = None  # Python < 3.6 does not handle closure vars here.
    kw_defaults = self._convert_kw_defaults(kw_defaults)
    annot, late_annot = (
        self.annotations_util.convert_function_annotations(state.node,
                                                           raw_annotations))
    return state, pos_defaults, kw_defaults, annot, late_annot, free_vars

  def _get_extra_function_args_3_6(self, state, arg):
    """Get function annotations and defaults from the stack (Python3.6+)."""
    free_vars = None
    pos_defaults = ()
    kw_defaults = {}
    annot = {}
    # TODO(mdemello): Does the 3.6 calling scheme need late annotations?
    late_annot = {}
    if arg & loadmarshal.MAKE_FUNCTION_HAS_FREE_VARS:
      state, free_vars = state.pop()
    if arg & loadmarshal.MAKE_FUNCTION_HAS_ANNOTATIONS:
      state, packed_annot = state.pop()
      annot = abstract.get_atomic_python_constant(packed_annot, dict)
      for k in annot.keys():
        annot[k] = abstract.get_atomic_value(annot[k])
    if arg & loadmarshal.MAKE_FUNCTION_HAS_KW_DEFAULTS:
      state, packed_kw_def = state.pop()
      kw_defaults = abstract.get_atomic_python_constant(packed_kw_def, dict)
    if arg & loadmarshal.MAKE_FUNCTION_HAS_POS_DEFAULTS:
      state, packed_pos_def = state.pop()
      pos_defaults = abstract.get_atomic_python_constant(packed_pos_def, tuple)
    return state, pos_defaults, kw_defaults, annot, late_annot, free_vars

  def _process_function_type_comment(self, op, code_var, annotations,
                                     late_annotations):
    """Modifies annotations/late_annotations from a function type comment.

    Checks if a type comment is present for the function.  If so, the type
    comment is used to populate late_annotations.  It is an error to have
    a type comment when annotations or late_annotations is not empty.

    Args:
      op: An opcode (used to determine filename and line number).
      code_var: A variable for functions's code object.
      annotations: A dict of annotations.
      late_annotations: A dict of late annotations.
    """
    # Find type comment (if any).  It should appear on the line immediately
    # following the opcode.
    filename = op.code.co_filename
    if filename != self.filename or op.line is None:
      return

    co_code = code_var.data[0].pyval.co_code
    if not co_code:
      return
    comment = None
    # Look for a type comment on a bare line after the opcode but before the
    # first actual function code.
    lineno = None
    for lineno in range(op.line + 1, co_code[0].line):
      entry = self.director.type_comments.get(lineno)
      # entry is either None, or (src, comment).
      if entry and not entry[0]:
        comment = entry[1]
        break
    if not comment:
      return

    # It is an error to use a type comment on an annotated function.
    if annotations or late_annotations:
      self.errorlog.redundant_function_type_comment(filename, lineno)
      return

    # Parse the comment, use a fake Opcode that is similar to the original
    # opcode except that it is set to the line number of the type comment.
    # This ensures that errors are printed with an accurate line number.
    fake_stack = self.simple_stack(op.at_line(lineno))
    m = _FUNCTION_TYPE_COMMENT_RE.match(comment)
    if not m:
      self.errorlog.invalid_function_type_comment(fake_stack, comment)
      return
    args, return_type = m.groups()

    # Add type info to late_annotations.
    if args != "...":
      annot = annotations_util.LateAnnotation(
          args.strip(), function.MULTI_ARG_ANNOTATION, fake_stack)
      late_annotations[function.MULTI_ARG_ANNOTATION] = annot

    ret = self.convert.build_string(None, return_type).data[0]
    late_annotations["return"] = annotations_util.LateAnnotation(
        ret, "return", fake_stack)

  def byte_MAKE_FUNCTION(self, state, op):
    """Create a function and push it onto the stack."""
    if self.python_version[0] == 2:
      name = None
    else:
      assert self.python_version[0] == 3
      state, name_var = state.pop()
      name = abstract.get_atomic_python_constant(name_var)
    state, code = state.pop()
    if self.python_version >= (3, 6):
      get_args = self._get_extra_function_args_3_6
    else:
      get_args = self._get_extra_function_args
    state, defaults, kw_defaults, annot, late_annot, free_vars = (
        get_args(state, op.arg))
    self._process_function_type_comment(op, code, annot, late_annot)
    # TODO(dbaum): Add support for per-arg type comments.
    # TODO(dbaum): Add support for variable type comments.
    globs = self.get_globals_dict()
    fn = self._make_function(name, state.node, code, globs, defaults,
                             kw_defaults, annotations=annot,
                             late_annotations=late_annot,
                             closure=free_vars)
    self.trace_functiondef(fn)
    return state.push(fn)

  def byte_MAKE_CLOSURE(self, state, op):
    """Make a function that binds local variables."""
    if self.python_version[0] == 2:
      # The py3 docs don't mention this change.
      name = None
    else:
      assert self.python_version[0] == 3
      state, name_var = state.pop()
      name = abstract.get_atomic_python_constant(name_var)
    state, (closure, code) = state.popn(2)
    # TODO(mdemello): Handle kw_defaults and annotations (Python 3.5).
    state, defaults, kw_defaults, _, _, _ = (
        self._get_extra_function_args(state, op.arg))
    globs = self.get_globals_dict()
    fn = self._make_function(name, state.node, code, globs, defaults,
                             kw_defaults, closure=closure)
    return state.push(fn)

  def byte_CALL_FUNCTION(self, state, op):
    return self.call_function_from_stack(state, op.arg, None, None)

  def byte_CALL_FUNCTION_VAR(self, state, op):
    state, args = self.pop_varargs(state)
    return self.call_function_from_stack(state, op.arg, args, None)

  def byte_CALL_FUNCTION_KW(self, state, op):
    state, kwargs = self.pop_kwargs(state)
    return self.call_function_from_stack(state, op.arg, None, kwargs)

  def byte_CALL_FUNCTION_VAR_KW(self, state, op):
    state, kwargs = self.pop_kwargs(state)
    state, args = self.pop_varargs(state)
    return self.call_function_from_stack(state, op.arg, args, kwargs)

  def byte_CALL_FUNCTION_EX(self, state, op):
    """Call a function."""
    if op.arg & loadmarshal.CALL_FUNCTION_EX_HAS_KWARGS:
      state, starstarargs = state.pop()
    else:
      starstarargs = None
    state, starargs = state.pop()
    state, fn = state.pop()
    # TODO(mdemello): fix abstract.FunctionArgs() to properly init namedargs,
    # and remove this.
    namedargs = abstract.Dict(self)
    state, ret = self.call_function_with_state(
        state, fn, (), namedargs=namedargs, starargs=starargs,
        starstarargs=starstarargs)
    return state.push(ret)

  def byte_YIELD_VALUE(self, state, op):
    """Yield a value from a generator."""
    state, ret = state.pop()
    self.frame.yield_variable.PasteVariable(ret, state.node)
    if self.frame.check_return:
      # Create a dummy generator instance for checking that
      # Generator[<yield_variable>] matches the annotated return type.
      generator = abstract.Generator(self.frame, self)
      generator.type_parameters[abstract.T] = self.frame.yield_variable
      self._check_return(state.node, generator.to_variable(state.node),
                         self.frame.allowed_returns)
    return state.set_why("yield")

  def byte_IMPORT_NAME(self, state, op):
    """Import a single module."""
    full_name = self.frame.f_code.co_names[op.arg]
    # The identifiers in the (unused) fromlist are repeated in IMPORT_FROM.
    state, (level_var, fromlist) = state.popn(2)
    if op.line in self.director.ignore:
      # "import name  # type: ignore"
      return state.push(self.convert.create_new_unsolvable(state.node))
    # The IMPORT_NAME for an "import a.b.c" will push the module "a".
    # However, for "from a.b.c import Foo" it'll push the module "a.b.c". Those
    # two cases are distinguished by whether fromlist is None or not.
    if self._is_only_none(state.node, fromlist):
      name = full_name.split(".", 1)[0]  # "a.b.c" -> "a"
    else:
      name = full_name
    level = abstract.get_atomic_python_constant(level_var)
    module = self.import_module(name, full_name, level)
    if module is None:
      log.warning("Couldn't find module %r", name)
      self.errorlog.import_error(self.frames, name)
      module = self.convert.unsolvable
    return state.push(module.to_variable(state.node))

  def byte_IMPORT_FROM(self, state, op):
    """IMPORT_FROM is mostly like LOAD_ATTR but doesn't pop the container."""
    if op.line in self.director.ignore:
      # "from x import y  # type: ignore"
      return state.push(self.convert.create_new_unsolvable(state.node))
    name = self.frame.f_code.co_names[op.arg]
    module = state.top()
    state, attr = self.load_attr_noerror(state, module, name)
    if attr is None:
      full_name = module.data[0].name + "." + name
      self.errorlog.import_error(self.frames, full_name)
      attr = self.convert.unsolvable.to_variable(state.node)
    return state.push(attr)

  def byte_EXEC_STMT(self, state, op):
    state, (unused_stmt, unused_globs, unused_locs) = state.popn(3)
    log.warning("Encountered 'exec' statement. 'exec' is unsupported.")
    return state

  def byte_BUILD_CLASS(self, state, op):
    state, (name, _bases, members) = state.popn(3)
    bases = list(abstract.get_atomic_python_constant(_bases))
    return state.push(self.make_class(state.node, name, bases, members, None))

  def byte_LOAD_BUILD_CLASS(self, state, op):
    # New in py3
    return state.push(abstract.BuildClass(self).to_variable(state.node))

  def byte_STORE_LOCALS(self, state, op):
    state, locals_dict = state.pop()
    self.frame.f_locals = abstract.get_atomic_value(locals_dict)
    return state

  def byte_END_FINALLY(self, state, op):
    state, exc = state.pop()
    if self._is_only_none(state.node, exc):
      return state
    else:
      log.info("Popping exception %r", exc)
      state = state.pop_and_discard()
      state = state.pop_and_discard()
      # If a pending exception makes it all the way out of an "except" block,
      # no handler matched, hence Python re-raises the exception.
      return state.set_why("reraise")

  def _check_return(self, node, actual, formal):
    pass  # overridden in infer.py

  def byte_RETURN_VALUE(self, state, op):
    """Get and check the return value."""
    state, var = state.pop()
    if self.frame.check_return:
      if self.frame.f_code.co_flags & loadmarshal.CodeType.CO_GENERATOR:
        # A generator shouldn't return anything, so the expected return type
        # is None.
        self._check_return(state.node, var, self.convert.none_type)
      else:
        self._check_return(state.node, var, self.frame.allowed_returns)
    if self.frame.allowed_returns is not None:
      _, _, retvar = self.init_class(state.node, self.frame.allowed_returns)
    else:
      retvar = var
    self.frame.return_variable.PasteVariable(retvar, state.node)
    return state.set_why("return")

  def byte_IMPORT_STAR(self, state, op):
    """Pops a module and stores all its contents in locals()."""
    # TODO(kramm): this doesn't use __all__ properly.
    state, mod_var = state.pop()
    mod = abstract.get_atomic_value(mod_var)
    # TODO(rechen): Is mod ever an unknown?
    if isinstance(mod, (abstract.Unknown, abstract.Unsolvable)):
      self.has_unknown_wildcard_imports = True
      return state
    log.info("%r", mod)
    # TODO(kramm): Add Module type to abstract.py
    for name, var in mod.items():
      if name[0] != "_" or name == "__getattr__":
        state = self.store_local(state, name, var)
    return state

  def byte_SLICE_0(self, state, op):
    return self.get_slice(state, 0)

  def byte_SLICE_1(self, state, op):
    return self.get_slice(state, 1)

  def byte_SLICE_2(self, state, op):
    return self.get_slice(state, 2)

  def byte_SLICE_3(self, state, op):
    return self.get_slice(state, 3)

  def byte_STORE_SLICE_0(self, state, op):
    return self.store_slice(state, 0)

  def byte_STORE_SLICE_1(self, state, op):
    return self.store_slice(state, 1)

  def byte_STORE_SLICE_2(self, state, op):
    return self.store_slice(state, 2)

  def byte_STORE_SLICE_3(self, state, op):
    return self.store_slice(state, 3)

  def byte_DELETE_SLICE_0(self, state, op):
    return self.delete_slice(state, 0)

  def byte_DELETE_SLICE_1(self, state, op):
    return self.delete_slice(state, 1)

  def byte_DELETE_SLICE_2(self, state, op):
    return self.delete_slice(state, 2)

  def byte_DELETE_SLICE_3(self, state, op):
    return self.delete_slice(state, 3)

  def byte_WITH_CLEANUP_START(self, state, op):
    """Called at the end of a with block. Calls the exit handlers etc."""
    # From the following commit: https://goo.gl/WxS2vJ
    # WITH_CLEANUP_START in ceval.c is just WITH_CLEANUP with a name change.
    return self.byte_WITH_CLEANUP(state, op)

  def byte_WITH_CLEANUP_FINISH(self, state, op):
    """Called when an async exit function returns."""
    # TODO(mdemello): Should we do something with the result here?
    state, unused_result = state.pop()
    # We don't pop the exception because we never pushed it in
    # WITH_CLEANUP_START.
    # Set WHY_SILENCED to prevent END_FINALLY from trying to reraise the
    # nonexistent exception
    state = state.set_why("silenced")
    return state

  def byte_SETUP_ANNOTATIONS(self, state, op):
    """Sets up variable annotations in locals()."""
    annotations = self.convert.build_map(state.node)
    return self.store_local(state, "__annotations__", annotations)

  def byte_STORE_ANNOTATION(self, state, op):
    state, annotations_var = self.load_local(state, "__annotations__")
    annotations = abstract.get_atomic_value(annotations_var)
    name = self.frame.f_code.co_names[op.arg]
    state, value = state.pop()
    annotations.set_str_item(state.node, name, value)
    return self.store_local(state, "__annotations__", annotations_var)

  def byte_GET_YIELD_FROM_ITER(self, state, op):
    # TODO(mdemello): We should check if TOS is a generator iterator or
    # coroutine first, and do nothing if it is, else call GET_ITER
    return self.byte_GET_ITER(state, op)

  def _pop_and_unpack_list(self, state, count):
    """Pop count iterables off the stack and concatenate."""
    state, elements = state.popn(count)
    # TODO(mdemello): We need to expand the iterables here.
    return state, elements

  def byte_BUILD_LIST_UNPACK(self, state, op):
    state, ret = self._pop_and_unpack_list(state, op.arg)
    ret = self.convert.build_list(state.node, ret)
    return state.push(ret)

  def byte_BUILD_MAP_UNPACK(self, state, op):
    # TODO(mdemello): We should actually populate the map.
    state, _ = self._pop_and_unpack_list(state, op.arg)
    return state.push(self.convert.build_map(state.node))

  def byte_BUILD_MAP_UNPACK_WITH_CALL(self, state, op):
    if self.python_version >= (3, 6):
      state, _ = self._pop_and_unpack_list(state, op.arg)
    else:
      state, _ = self._pop_and_unpack_list(state, op.arg & 0xff)
    return state.push(self.convert.build_map(state.node))

  def byte_BUILD_TUPLE_UNPACK(self, state, op):
    state, ret = self._pop_and_unpack_list(state, op.arg)
    return state.push(self.convert.build_tuple(state.node, ret))

  def byte_BUILD_TUPLE_UNPACK_WITH_CALL(self, state, op):
    return self.byte_BUILD_TUPLE_UNPACK(state, op)

  def byte_BUILD_SET_UNPACK(self, state, op):
    state, ret = self._pop_and_unpack_list(state, op.arg)
    return state.push(self.convert.build_set(state.node, ret))

  def byte_SETUP_ASYNC_WITH(self, state, op):
    state, res = state.pop()
    level = len(state.data_stack)
    state = self.push_block(state, "finally", op, op.target, level)
    return state.push(res)

  def byte_FORMAT_VALUE(self, state, op):
    if op.arg & loadmarshal.FVS_MASK:
      state = state.pop_and_discard()
    # FORMAT_VALUE pops, formats and pushes back a string, so we just need to
    # push a new string onto the stack.
    state = state.pop_and_discard()
    ret = abstract.Instance(self.convert.str_type, self)
    return state.push(ret.to_variable(state.node))

  def byte_BUILD_CONST_KEY_MAP(self, state, op):
    state, keys = state.pop()
    keys = abstract.get_atomic_python_constant(keys, tuple)
    the_map = self.convert.build_map(state.node)
    assert len(keys) == op.arg
    for key in reversed(keys):
      state, val = state.pop()
      state = self.store_subscr(state, the_map, key, val)
    return state.push(the_map)

  def byte_BUILD_STRING(self, state, op):
    # TODO(mdemello): Test this.
    state, _ = state.popn(op.arg)
    ret = abstract.Instance(self.convert.str_type, self)
    return state.push(ret.to_variable(state.node))

  def byte_GET_AITER(self, state, op):
    # We don't support this feature yet; simply don't crash.
    return state

  def byte_GET_ANEXT(self, state, op):
    # We don't support this feature yet; simply don't crash.
    ret = abstract.Instance(self.convert.function_type, self)
    return state.push(ret.to_variable(state.node))

  def byte_BEFORE_ASYNC_WITH(self, state, op):
    # We don't support this feature yet; simply don't crash.
    # Pop a context manager and push its enter and exit methods.
    state, _ = state.pop()
    meth = abstract.Instance(self.convert.function_type, self)
    state = state.push(meth.to_variable(state.node))
    state = state.push(meth.to_variable(state.node))
    return state

  def byte_GET_AWAITABLE(self, state, op):
    # We don't support this feature yet; simply don't crash.
    return state

  def byte_YIELD_FROM(self, state, op):
    # We don't support this feature yet; simply don't crash.
    return state.pop_and_discard()
