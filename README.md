# BiliBatchDownloader 作者作品库

中文 Windows 桌面应用：**本地作者 → 分类 → 作品 → 分P与实际截图 → 默认播放器**。当前 **0.1.0 VERIFY候选版**，采用浅色卡片、B站粉色点缀，参考 PixivDesk 的三级浏览和后台任务模式。

本轮从仅研究探针推进到可操作作品库；按用户批准的模块方式交付，整体研究仍 **NEEDS MORE EVIDENCE**。已有功能与最终完整需求分别记录，不把固定小样本成功外推到全部B站内容。

## 当前能做什么

- SQLite离线作品库，按UID/BV/CID去重；本地作者分组，按作者、标题、正文、原标签搜索，分类与未下载筛选、40项分页。
- 作品卡片、分P详情、原站信息与标签状态、最多6张带实际帧时间的内容截图；调用系统默认播放器及文件夹，缺失/变化明确提示。本地打开独立于下载队列，不启动下载worker。
- 读取BV或视频链接后选择分P；yt-dlp **标准 `extract_info(download=True)` 流程**负责下载、格式选择与合并，应用没有覆盖 `urlopen` 或自建媒体传输。
- 独立worker和Windows Job；串行持久化任务、暂停视频派发、取消、人工重试、重启后中断标记。核验后同卷无覆盖发布；视频下载与截图失败分开。
- 导入本工具已有核验报告：复核视频/截图哈希，原文件原位登记，不扫描旧素材目录。

## 验证与限制

原生固定普通视频P1一次验证 **PASS**：7次请求、一次301、37,708,058 HTTP传输字节、360p音视频合并和完整解码。此前三个视频/18张预览及用户播放验收继续保留；新原生输出尚未由用户重新播放确认。

每任务暂限 **12次请求、230秒、15秒单次网络等待、最高360p、64MiB总HTTP传输、零自动重试**。元数据与响应头也计入字节上限。达到限制或出现登录/平台限制立即停止，不扩大预算。截图未通过不会抹掉已核验的视频。

尚未完成：本人关注的产品同步、完整作者索引、图库/GIF/实况照片/独立动态视频、转发归属、头像/原站封面缓存、通用旧目录预扫描、跨启动无缝续传。下载中心的取消和重启恢复已做合成检查，真实网络中断场景仍待验证。

最新工程、包测试及CI见[0.1.0交付记录](docs/0.1.0交付记录.md)。用户已确认r5的界面、浏览与播放入口正常，并反馈播放打开偏慢；r7移除了应用侧队列和worker等待，夸克实际启动耗时仍需体验确认。无Python的干净Windows与完整可交互首屏≤2秒尚未验收。

## 启动与使用

本机候选包：`dist/BiliBatchDownloader-0.1.0-verify-r7/BiliBatchDownloader.exe`。先关闭旧版窗口，再打开新版本；保留同目录的 `BiliDeskWorker.exe` 与 `_internal`，不要只复制一个EXE。打开同目录 `使用手册.html` 可离线阅读。

本地数据库与设置位于 `%LOCALAPPDATA%/BiliBatchDownloaderDesk`，不随程序目录或公开源码分发。本机已将授权的3个视频、18张截图原位登记；公开包为空库，不包含用户媒体。

联网前在设置中填写本人专用Chrome档案、下载目录及独立FFmpeg/FFprobe工具对。先在旧工具的专用Chrome完成登录，再关闭浏览器。Cookie仅在worker内存中读取，应用不保存Cookie文件。没有捆绑FFmpeg、gallery-dl或PixivUtil2到本次便携包。

新下载按“作者UID/内容分类/BV/CID”组织。图文／图片、投稿视频、动态视频、待分类四类保留；来源证据不足默认待分类。手工纠正分类记录人工依据，但不移动旧文件。不能凭BV、扩展名或动态入口判断投稿属性。

## 源码运行与检查

```powershell
cd D:\sd.webui\bili_desktop
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.lock.txt
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m bilidesk.app
```

离线测试不访问B站或真实Chrome档案。合成媒体测试需显式指定FFmpeg，缺失时相应用例会跳过，不能写作全通过。

```powershell
$env:BILI_TEST_FFMPEG = '你的独立工具目录\ffmpeg.exe'
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.artifacts/pytest-new-run
.\.venv\Scripts\python.exe scripts/benchmark_library.py --output .artifacts/benchmark-new-run
```

每次使用新的输出目录，保留旧失败证据。打包使用 `scripts/build_windows.py --name BiliBatchDownloader-0.1.0-新标识`；需先准备 `build/license-cache` 中完整GPLv3/LGPLv3文本，构建绝不覆盖旧目录。详见[第三方边界](docs/第三方依赖.md)。

## 项目维护

- [模块研究与设计评审](docs/视频作品库模块评审.md)、[完整功能与验收矩阵](docs/功能与验收矩阵.md)、[Issue #1](https://github.com/awa606/BiliBatchDownloader/issues/1)。
- [历史研究结果](docs/研究结果-20261016.md)、[历史研究工具说明](docs/历史研究工具说明-20261016.md)、[预注册](docs/研究预注册.md)。旧失败不删除，图片/实况本轮没有恢复请求。
- 自有源码 [MIT](LICENSE)。公开Git只含源码、合成测试、文档和脱敏结论；媒体、截图、数据库、浏览器档案、凭据、环境、日志和便携包均被忽略。
- 按模块研究和设计通过后编码，完成工程检查先进入VERIFY。整体研究和最终人工验收不随局部成功自动通过。

## 参考方案

[yt-dlp官方嵌入](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp)、[gallery-dl B站提取器](https://github.com/mikf/gallery-dl/blob/v1.32.16/gallery_dl/extractor/bilibili.py)、[PixivUtil2](https://github.com/Nandaka/PixivUtil2)、[Qt进程模型](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QProcess.html)。Douzy仅为用户指定的体验参考，未确认可复用桌面源码；不把其他抖音CLI项目标作Douzy。
