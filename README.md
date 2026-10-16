# BiliBatchDownloader

面向 Windows 的 B站作者作品库升级项目，目标是关注同步、作者浏览、视频与动态下载、作品预览和本地打开。

**当前阶段：研究验证中，尚无新版可运行产品。** 2026-10-16 重新登录后，本人关注完整分页通过；普通视频、多P视频、用户多图及实况图的元数据已获取。首次真实视频下载在页面请求收到 **HTTP412**，0媒体，已停止本轮全部B站在线实验。旧程序继续保留使用。

## 已完成与待完成

- 已完成：独立工程与公开仓库、锁定研究环境、真实登录与完整关注分页、普通视频与多P元数据、脱敏证据和离线测试。
- 待验证：真实媒体下载与音轨合并、动态图集、真实动图、动态视频、转发归属、作者完整作品分页。
- 待实现：新版界面、SQLite 作品库、后台队列、内容截图、旧文件原位导入、便携 EXE。
- 门禁：Research `NEEDS MORE EVIDENCE`；账号前提 `PASS`；在线实验 `BLOCKED_HTTP412`；Design `NOT_STARTED`。
- 本地工程检查：175项离线测试、源码编译和依赖一致性检查通过；不等于真实下载或产品验收。

首版需求与验收见[功能与验收矩阵](docs/功能与验收矩阵.md)和[维护Issue #1](https://github.com/awa606/BiliBatchDownloader/issues/1)。当前证据见[研究结果](docs/研究结果-20261016.md)，实验顺序见[研究预注册](docs/研究预注册.md)，候选比较和空决策见[研究决策记录](docs/研究决策记录.md)。

## 运行离线检查

需要 Python 3.13。以下检查不会访问 B站、浏览器档案或下载媒体。

```powershell
cd D:\sd.webui\bili_desktop
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-research.lock.txt
New-Item -ItemType Directory -Path .artifacts -Force | Out-Null
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.artifacts/pytest-manual-01
```

每轮使用新的 `--basetemp` 路径，保留失败证据。首次默认系统临时目录遇到权限错误，改用本项目实验目录后通过，未修改系统权限。

## 运行明确授权的账号验证

**当前在线实验因HTTP412停止。** 以下是工具运行说明，不是自动重跑指令；须先确认平台限制前提已解决并登记下一轮实验。重新登录已经完成，本次412不能再次简单归因于未登录。

先在旧工具点击“打开专用 Chrome”，完成B站登录并关闭该浏览器，再使用明确指定的专用档案。不要发送 Cookie、密码或账号截图。

```powershell
.\.venv\Scripts\python.exe experiments/research_probe.py `
  --profile 'D:\sd.webui\tools\bili_download\chrome_bili_profile' `
  --followings `
  --output .artifacts/U01-account-new-attempt.json
```

也支持用 `--cookie-file` 指定本人提供的 Netscape Cookie 文件，不能与 `--profile` 同用。凭据仅进入进程内存，输出只保留业务码、计数和响应哈希。证据文件只允许写入 `.artifacts`，已有结果不能覆盖。

默认实验先验证真实登录，再取关注前两页；总数超过250时额外核验第5/6页。最多5次请求，单次网络等待15秒，无自动重试。有限分页结果必须标记 `PARTIAL`。边界通过后，可用预注册的 `--full-pages N` 做最多N页完整性核验；本次限定8页，结果与平台总量一致，重复为0。返回登录失效、平台限制或结构异常时停止，保留失败，再登记有明确假设的新实验。

视频、图集和动态探针位于 `experiments/`，不是应用下载入口。视频元数据探针只检查前两P的播放信息，并读取整组CID；禁止据此声称全部分P下载通过。原上游图集404失败保留；用户新样本读到4PNG及9JPG+9配套MP4，但真实媒体未验证，实况照片也不等同GIF。

图集下载探针还要求明确提供的FFmpeg旁存在FFprobe，缺失时在联网前停止；本机该前提未满足。作者索引探针最多2页，仅完成离线检查；其COMPLETE只描述服务器本次可见快照，不证明所有历史或隐藏内容完整。

## 公开仓库边界

公开内容仅包含自有研究代码、测试、文档和公开上游样本链接。`.venv`、`.artifacts`、凭据、浏览器档案、数据库、下载媒体及旧软件包不进入 Git。CI 只运行合成离线测试，不使用账号秘密或真实站点。

自有代码采用 [MIT](LICENSE)。第三方依赖保留各自许可；当前没有分发第三方二进制、媒体或新版 EXE。许可调查见[第三方依赖](docs/第三方依赖.md)。

## 参考资料

- [Douzy：产品流程参考](https://github.com/jiji262/douyin-downloader)
- [yt-dlp：视频候选](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19)
- [gallery-dl：图文候选](https://github.com/mikf/gallery-dl/releases/tag/v1.32.16)
