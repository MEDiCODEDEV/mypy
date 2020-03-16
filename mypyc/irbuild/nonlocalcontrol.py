"""Helpers for dealing with nonlocal control such as 'break' and 'return'.

Model how these behave differently in different contexts.
"""

from abc import abstractmethod
from typing import Optional, Union
from typing_extensions import TYPE_CHECKING

from mypyc.ir.ops import (
    Branch, BasicBlock, Unreachable, Value, Goto, LoadInt, Assign, Register, Return,
    AssignmentTarget, NO_TRACEBACK_LINE_NO
)
from mypyc.primitives.exc_ops import set_stop_iteration_value, restore_exc_info_op

if TYPE_CHECKING:
    from mypyc.irbuild.builder import IRBuilder


class NonlocalControl:
    """ABC representing a stack frame of constructs that modify nonlocal control flow.

    The nonlocal control flow constructs are break, continue, and
    return, and their behavior is modified by a number of other
    constructs.  The most obvious is loop, which override where break
    and continue jump to, but also `except` (which needs to clear
    exc_info when left) and (eventually) finally blocks (which need to
    ensure that the finally block is always executed when leaving the
    try/except blocks).
    """

    @abstractmethod
    def gen_break(self, builder: 'IRBuilder', line: int) -> None: pass

    @abstractmethod
    def gen_continue(self, builder: 'IRBuilder', line: int) -> None: pass

    @abstractmethod
    def gen_return(self, builder: 'IRBuilder', value: Value, line: int) -> None: pass


class BaseNonlocalControl(NonlocalControl):
    """Default nonlocal control outside any statements that affect it."""

    def gen_break(self, builder: 'IRBuilder', line: int) -> None:
        assert False, "break outside of loop"

    def gen_continue(self, builder: 'IRBuilder', line: int) -> None:
        assert False, "continue outside of loop"

    def gen_return(self, builder: 'IRBuilder', value: Value, line: int) -> None:
        builder.add(Return(value))


class LoopNonlocalControl(NonlocalControl):
    """Nonlocal control within a loop."""

    def __init__(self,
                 outer: NonlocalControl,
                 continue_block: BasicBlock,
                 break_block: BasicBlock) -> None:
        self.outer = outer
        self.continue_block = continue_block
        self.break_block = break_block

    def gen_break(self, builder: 'IRBuilder', line: int) -> None:
        builder.add(Goto(self.break_block))

    def gen_continue(self, builder: 'IRBuilder', line: int) -> None:
        builder.add(Goto(self.continue_block))

    def gen_return(self, builder: 'IRBuilder', value: Value, line: int) -> None:
        self.outer.gen_return(builder, value, line)


class GeneratorNonlocalControl(BaseNonlocalControl):
    """Default nonlocal control in a generator function outside statements."""

    def gen_return(self, builder: 'IRBuilder', value: Value, line: int) -> None:
        # Assign an invalid next label number so that the next time
        # __next__ is called, we jump to the case in which
        # StopIteration is raised.
        builder.assign(builder.fn_info.generator_class.next_label_target,
                       builder.add(LoadInt(-1)),
                       line)

        # Raise a StopIteration containing a field for the value that
        # should be returned. Before doing so, create a new block
        # without an error handler set so that the implicitly thrown
        # StopIteration isn't caught by except blocks inside of the
        # generator function.
        builder.builder.push_error_handler(None)
        builder.goto_and_activate(BasicBlock())

        # Skip creating a traceback frame when we raise here, because
        # we don't care about the traceback frame and it is kind of
        # expensive since raising StopIteration is an extremely common
        # case.  Also we call a special internal function to set
        # StopIteration instead of using RaiseStandardError because
        # the obvious thing doesn't work if the value is a tuple
        # (???).
        builder.primitive_op(set_stop_iteration_value, [value], NO_TRACEBACK_LINE_NO)
        builder.add(Unreachable())
        builder.builder.pop_error_handler()


class CleanupNonlocalControl(NonlocalControl):
    """Abstract nonlocal control that runs some cleanup code. """

    def __init__(self, outer: NonlocalControl) -> None:
        self.outer = outer

    @abstractmethod
    def gen_cleanup(self, builder: 'IRBuilder', line: int) -> None: ...

    def gen_break(self, builder: 'IRBuilder', line: int) -> None:
        self.gen_cleanup(builder, line)
        self.outer.gen_break(builder, line)

    def gen_continue(self, builder: 'IRBuilder', line: int) -> None:
        self.gen_cleanup(builder, line)
        self.outer.gen_continue(builder, line)

    def gen_return(self, builder: 'IRBuilder', value: Value, line: int) -> None:
        self.gen_cleanup(builder, line)
        self.outer.gen_return(builder, value, line)


class TryFinallyNonlocalControl(NonlocalControl):
    """Nonlocal control within try/finally."""

    def __init__(self, target: BasicBlock) -> None:
        self.target = target
        self.ret_reg = None  # type: Optional[Register]

    def gen_break(self, builder: 'IRBuilder', line: int) -> None:
        builder.error("break inside try/finally block is unimplemented", line)

    def gen_continue(self, builder: 'IRBuilder', line: int) -> None:
        builder.error("continue inside try/finally block is unimplemented", line)

    def gen_return(self, builder: 'IRBuilder', value: Value, line: int) -> None:
        if self.ret_reg is None:
            self.ret_reg = builder.alloc_temp(builder.ret_types[-1])

        builder.add(Assign(self.ret_reg, value))
        builder.add(Goto(self.target))


class ExceptNonlocalControl(CleanupNonlocalControl):
    """Nonlocal control for except blocks.

    Just makes sure that sys.exc_info always gets restored when we leave.
    This is super annoying.
    """

    def __init__(self, outer: NonlocalControl, saved: Union[Value, AssignmentTarget]) -> None:
        super().__init__(outer)
        self.saved = saved

    def gen_cleanup(self, builder: 'IRBuilder', line: int) -> None:
        builder.primitive_op(restore_exc_info_op, [builder.read(self.saved)], line)


class FinallyNonlocalControl(CleanupNonlocalControl):
    """Nonlocal control for finally blocks.

    Just makes sure that sys.exc_info always gets restored when we
    leave and the return register is decrefed if it isn't null.
    """

    def __init__(self, outer: NonlocalControl, ret_reg: Optional[Value], saved: Value) -> None:
        super().__init__(outer)
        self.ret_reg = ret_reg
        self.saved = saved

    def gen_cleanup(self, builder: 'IRBuilder', line: int) -> None:
        # Do an error branch on the return value register, which
        # may be undefined. This will allow it to be properly
        # decrefed if it is not null. This is kind of a hack.
        if self.ret_reg:
            target = BasicBlock()
            builder.add(Branch(self.ret_reg, target, target, Branch.IS_ERROR))
            builder.activate_block(target)

        # Restore the old exc_info
        target, cleanup = BasicBlock(), BasicBlock()
        builder.add(Branch(self.saved, target, cleanup, Branch.IS_ERROR))
        builder.activate_block(cleanup)
        builder.primitive_op(restore_exc_info_op, [self.saved], line)
        builder.goto_and_activate(target)
