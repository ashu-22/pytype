"""Tests for generators."""

from pytype.tests import test_inference


class GeneratorTest(test_inference.InferenceTest):
  """Tests for iterators, generators, coroutines, and yield."""

  def testNext(self):
    ty = self.Infer("""
      def f():
        return next(i for i in [1,2,3])
    """, deep=True)
    self.assertTypesMatchPytd(ty, """
      def f() -> int
    """)

  def testList(self):
    ty = self.Infer("""
      y = list(x for x in [1, 2, 3])
    """, deep=True)
    self.assertTypesMatchPytd(ty, """
      from typing import List
      y = ...  # type: List[int, ...]
    """)

  def testReuse(self):
    ty = self.Infer("""
      y = list(x for x in [1, 2, 3])
      z = list(x for x in [1, 2, 3])
    """, deep=True)
    self.assertTypesMatchPytd(ty, """
      from typing import List
      y = ...  # type: List[int, ...]
      z = ...  # type: List[int, ...]
    """)

  def testNextWithDefault(self):
    ty = self.Infer("""
      def f():
        return next((i for i in [1,2,3]), None)
    """, deep=True)
    self.assertTypesMatchPytd(ty, """
      def f() -> int or NoneType
    """)

  def testIterMatch(self):
    ty = self.Infer("""
      class Foo(object):
        def bar(self):
          for x in __any_object__:
            return x
        def __iter__(self):
          return generator()
    """, deep=True)
    self.assertTypesMatchPytd(ty, """
      from typing import Generator
      class Foo(object):
        def bar(self) -> ?
        def __iter__(self) -> Generator[nothing, nothing, nothing]
    """)

  def testCoroutineType(self):
    ty = self.Infer("""
      def foo(self):
        yield 3
    """, deep=True)
    self.assertTypesMatchPytd(ty, """
      from typing import Any, Generator
      def foo(self) -> Generator[int, Any, Any]
    """)

  def testIterationOfGetItem(self):
    ty = self.Infer("""
      class Foo(object):
        def __getitem__(self, key):
          return "hello"

      def foo(self):
        for x in Foo():
          return x
    """, deep=True)
    self.assertTypesMatchPytd(ty, """
      from typing import Union
      class Foo(object):
        def __getitem__(self, key) -> str
      def foo(self) -> Union[None, str]
    """)

  def testUnpackingOfGetItem(self):
    ty = self.Infer("""
      class Foo(object):
        def __getitem__(self, pos):
          if pos < 3:
            return pos
          else:
            raise StopIteration
      x, y, z = Foo()
    """, deep=True)
    self.assertTypesMatchPytd(ty, """
      from typing import Any, TypeVar
      _T0 = TypeVar("_T0")
      class Foo(object):
        def __getitem__(self, pos: _T0) -> _T0
      x = ...  # type: int
      y = ...  # type: int
      z = ...  # type: int
    """)

  def testReturnBeforeYield(self):
    self.assertNoErrors("""
      from __future__ import google_type_annotations
      from typing import Generator
      def f() -> generator:
        if __random__:
          return
        yield 5
    """)

  def testNoReturn(self):
    _, errors = self.InferAndCheck("""\
      from __future__ import google_type_annotations
      from typing import Generator
      def f() -> Generator[str]:
        yield 42
    """)
    self.assertErrorLogIs(errors, [(4, "bad-return-type", r"str.*int")])


if __name__ == "__main__":
  test_inference.main()
