"""魔搭创空间入口。

平台两条硬规矩写在这里，别处不重复：
  · 服务必须监听 0.0.0.0:7860（创空间的路由前提）；
  · 后端不得占用 Authorization / X-modelscope-* / X-studio-* 请求头。
"""
from playtest.ui import launch

if __name__ == "__main__":
    launch()
