from colab_mcp import runtime

def test_add():
    assert runtime.eval("1+2") == 3
