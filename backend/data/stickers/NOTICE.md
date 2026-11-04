# 贴纸素材来源与许可

本目录下的 `.svg` 文件来自 **Twemoji**。

- 项目：https://github.com/jdecked/twemoji （原 Twitter Twemoji 的社区维护分支）
- 版本：15.0.0（通过 npm 包 `@twemoji/svg@15.0.0` 获取）
- 许可：**CC-BY 4.0** https://creativecommons.org/licenses/by/4.0/

图形版权归 Twitter, Inc. 及 Twemoji 贡献者所有，本仓库仅按 CC-BY 4.0 使用，
不做二次创作。如果分发本项目，请保留本文件。

## 为什么内置而不是走 CDN

原本的实现是首次访问时从 `cdn.jsdelivr.net` 拉取并缓存到本地。实测该域名在部分
网络环境下连接会被重置，导致贴纸永远加载不出来。所以把当前用到的 18 个码点直接
内置进来（合计约 17 KB）——离线也能用，前端不依赖任何外部 CDN。

新增贴纸时，从 `@twemoji/svg` 里拷对应的 `<codepoint>.svg` 放进本目录即可。
文件名就是小写十六进制码点，多码点用 `-` 连接（例如 `2764-fe0f.svg`）。