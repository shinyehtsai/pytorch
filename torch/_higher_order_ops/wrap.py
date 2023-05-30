from torch._ops import HigherOrderOperator
from torch.utils.checkpoint import checkpoint

# Used for testing the HigherOrderOperator mechanism
class Wrap(HigherOrderOperator):
    def __init__(self):
        super().__init__("wrap")

    def __call__(self, func, *args):
        result = func(*args)
        return result

wrap = Wrap()

class WrapActivationCheckpoint(HigherOrderOperator):
    """
    Wrap activation checkpoint is little different from Wrap. There are two
    functions to wrap here - utils.checkpoint() itself and the function to be
    checkpointed (the first arg to the utils.checkpoint() function).
    """
    def __init__(self):
        super().__init__("wrap_activation_checkpoint")

    def __call__(self, function, *args, **kwargs):
        # use_reentrant is set to False because this op is going to be traced.
        # And we ensure that AOT Autograd traces through the non reentrant
        # version of checkpointing.
        kwargs["use_reentrant"] = False
        return checkpoint(function, *args, **kwargs)

wrap_activation_checkpoint = WrapActivationCheckpoint()


class TagActivationCheckpoint(HigherOrderOperator):
    """
    This operator is supposed to be used only with torch.compile stack. This
    accepts a Fx graph module which needs to be checkpointed. This operator adds
    "recomputable" tag to the nodes of the Fx graph that should be recomputed.
    This goal is to pass this information to AOT Autograd's partitioner which
    will actually perform the cut.
    """

    def __init__(self):
        super().__init__("wrap_activation_checkpoint")

    def tag_nodes(self, gmod):
        # TODO - This needs major investigation. Currently, we are tagging all
        # the forward nodes as recomputable. However, torch.utils.checkpoint
        # provides a custom function to selectively recompute. We will have to
        # figure out how to tag seletively.
        for node in gmod.graph.nodes:
            if node.op == "call_function":
                node.meta["recompute"] = True
        return gmod

    def __call__(self, gmod, *args, **kwargs):
        if "context_fn" in kwargs:
            raise RuntimeError("Tagged Activation checkpointing does not support selective checkpointing yet.")
        import torch.fx.traceback as fx_traceback
        from torch.fx import Interpreter
        gmod = self.tag_nodes(gmod)
        with fx_traceback.preserve_node_meta():
            return Interpreter(gmod).run(*args)

tag_activation_checkpoint = TagActivationCheckpoint()
