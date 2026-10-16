# BiliBatchDownloader

面向 Windows 的 B站作者作品库升级项目，目标是关注同步、作者浏览、视频与动态下载、作品预览和本地打开。

**当前阶段：研究验证中，尚无新版可运行产品。** 2026-10-16 修复了下载探针遗漏yt-dlp默认请求头的问题。正常浏览器访问固定页后，预注册单页实验的账号检查通过，但视频页返回 **HTTP301**；按零重定向规则停止，共2次请求、0媒体。原HTTP412失败继续保留，尚未恢复真实下载；旧程序继续保留使用。

## 已完成与待完成

- 已完成：独立工程与公开仓库、锁定研究环境、真实登录与完整关注分页、普通视频与多P元数据、脱敏证据和离线测试。
- 待验证：真实媒体下载与音轨合并、动态图集、真实动图、动态视频、转发归属、作者完整作品分页。
- 待实现：新版界面、SQLite 作品库、后台队列、内容截图、旧文件原位导入、便携 EXE。
- 门禁：Research `NEEDS MORE EVIDENCE`；账号前提 `PASS`；最新单页实验 `BLOCKED_REDIRECT301`；Design `NOT_STARTED`。
- 工程检查：本轮本地204项离线测试、源码编译和依赖检查通过；本轮Windows CI待推送后核验。[上一基线CI](https://github.com/awa606/BiliBatchDownloader/actions/runs/38885154980)为175项通过，不能替代本轮代码或真实下载验收。

首版需求与验收见[功能与验收矩阵](docs/功能与验收矩阵.md)和[维护Issue #1](https://github.com/awa606/BiliBatchDownloader/issues/1)。当前证据见[研究结果](docs/研究结果-20261016.md)，实验顺序见[研究预注册](docs/研究预注册.md)，候选比较和空决策见[研究决策记录](docs/研究决策记录.md)。

## 未来的内容管理方式

按“作者 → 内容分类 → 作品”浏览和管理新下载，分类为**图文／图片、投稿视频、动态视频、待分类**。这是已确认的产品要求，当前尚未实现界面或文件归档。

- 图文、多图、GIF和实况照片归入图文／图片；实况照片的图片及配套视频成组保存。
- 投稿视频与动态视频依据可核验的原站信息区分。不能只看扩展名、有无BV号、发现入口，或一次分页是否找到；证据不足保持待分类并允许纠正。
- 普通投稿分享到动态后仍关联同一作品；相同BV/CID多入口不重复下载。转发者与原作者分别记录。
- 作者身份以UID保持稳定；旧文件继续原位登记，不因新增分类自动移动或覆盖。纯文字与无独立媒体的转发记录仍保留在作者全部动态浏览中。

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

**当前在线实验已停止。** 首次下载HTTP412保留；本次正常浏览器前提确认后，单页探针在HTTP301停止，未跟随跳转、未执行后续三资产下载。以下是工具运行说明，不是自动重跑指令；下一项需先研究规范URL/重定向依据并重新登记实验。账号本次仍通过，不要求重复登录。

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

## 单页验证工具

独立的`experiments/video_page_probe.py`只验证固定BV13x41117TL：真实登录后读取一次页面、核对HTML内BV/标题，最多2请求、0重试、0重定向、60秒工作进程时限。必须先独立确认正常浏览器访问并传入`--browser-confirmed`；这个参数是操作者声明，不会自行证明访问前提。`--output`必须是`.artifacts`内的新文件。原HTML、标题文本、Cookie和原始异常不写入报告，成功也只代表页面可读。

## 公开仓库边界

公开内容仅包含自有研究代码、测试、文档和公开上游样本链接。`.venv`、`.artifacts`、凭据、浏览器档案、数据库、下载媒体及旧软件包不进入 Git。CI 只运行合成离线测试，不使用账号秘密或真实站点。

自有代码采用 [MIT](LICENSE)。第三方依赖保留各自许可；当前没有分发第三方二进制、媒体或新版 EXE。许可调查见[第三方依赖](docs/第三方依赖.md)。

## 参考资料

- [Douzy：产品流程参考](https://github.com/jiji262/douyin-downloader)
- [yt-dlp：视频候选](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19)
- [gallery-dl：图文候选](https://github.com/mikf/gallery-dl/releases/tag/v1.32.16)
