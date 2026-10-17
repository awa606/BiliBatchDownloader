# BiliBatchDownloader 作者作品库

中文 Windows 桌面应用：**本地作者 → 分类 → 作品 → 分P截图／成组图片与实况 → 本地打开**。当前 **0.2.0 VERIFY离线图库候选版**，采用浅色卡片、B站粉色点缀，参考 PixivDesk 的三级浏览和后台任务模式。

本轮在视频作品库基础上交付离线图库；按用户批准的模块方式交付，整体研究仍 **NEEDS MORE EVIDENCE**。已有功能与最终完整需求分别记录，不把固定小样本成功外推到全部B站内容。

## 0.3.0 图库联网候选开发

已实现单条动态读取→真实正文/标签及照片预览→手动选组（默认不勾选）→成组下载归档。沿用gallery-dl 1.32.16原生流程，独立Qt-free worker；实况配套视频仅请求所选组，不新增自动角色识别。布局保持现有结构。

本轮检查状态与启动方式见[0.3.0交付记录](docs/0.3.0交付记录.md)及[模块评审](docs/图库联网模块评审.md)。当前已通过体验验收的版本仍为0.2.0；新候选必须通过独立在线、冻结与分发检查后才提供，不把实现完成当作验收通过。

## 0.2.0 本轮交付

图库模块研究与设计通过，详见[评审](docs/图库模块评审.md)与[交付记录](docs/0.2.0交付记录.md)。用户已确认9组实况完整播放且画面与照片对应，并确认新界面、成组预览和本地打开正常。新模块为独立离线候选，保留原r8。

[下载0.2.0 VERIFY预发布](https://github.com/awa606/BiliBatchDownloader/releases/tag/v0.2.0-verify-r1)：公开包为空库；本机已有作品请使用下方明确数据库的启动入口。ZIP已从GitHub完整取回并核对SHA-256。

- SQLite v2新增图库作品/资产；动态图集ID与视频BV/CID分开，同作者下统一分类、搜索、分页。
- 已核验图库报告预扫描→确认→重新核验→事务登记；原位引用，重复不新增，缺失/变化/身份冲突整批拒绝。
- 有序图片及实况分组、方向校正和有界缓存、单播放器按需播放/暂停/重播；切组或离开立即停止并释放源。
- 打开原图、默认播放器和文件夹，独立后台核对路径、大小与SHA。未知作者名、标题、正文和时间保持待获取。

## 当前能做什么

- SQLite离线作品库，按UID/BV/CID去重；本地作者分组，按作者、标题、正文、原标签搜索，分类与未下载筛选、40项分页。
- 作品卡片、分P详情、原站信息与标签状态、最多6张带实际帧时间的内容截图；调用系统默认播放器及文件夹，缺失/变化明确提示。本地打开独立于下载队列，不启动下载worker。
- 原r8视频入口支持读取BV或视频链接后选择分P（0.2.0离线候选禁用联网派发）；yt-dlp **标准 `extract_info(download=True)` 流程**负责下载、格式选择与合并，应用没有覆盖 `urlopen` 或自建媒体传输。
- 独立worker和Windows Job；串行持久化任务、暂停视频派发、取消、人工重试、重启后中断标记。核验后同卷无覆盖发布；视频下载与截图失败分开。
- 导入本工具已有核验报告：复核视频/截图哈希，原文件原位登记，不扫描旧素材目录。

## 验证与限制

原生固定普通视频P1一次验证 **PASS**：7次请求、一次301、37,708,058 HTTP传输字节、360p音视频合并和完整解码。此前三个视频/18张预览及用户播放验收继续保留；新原生输出尚未由用户重新播放确认。

每任务暂限 **12次请求、230秒、15秒单次网络等待、最高360p、64MiB总HTTP传输、零自动重试**。元数据与响应头也计入字节上限。达到限制或出现登录/平台限制立即停止，不扩大预算。截图未通过不会抹掉已核验的视频。

固定4PNG及9组JPG＋MP4已通过gallery-dl原生下载、身份/配对/顺序/SHA及完整解码。13图默认查看和9组默认播放器完整播放已验收；本轮接入离线图库，工程检查、冻结产品与新界面人工体验均有独立通过记录。

尚未完成：本人关注的产品同步、完整作者索引、联网图库下载、GIF、独立动态视频、转发归属、可靠投稿分类依据、头像/原站封面缓存、通用旧目录扫描及跨启动无缝续传。新增图库不扩展视频联网验证范围。

最新工程、包测试及CI见[0.2.0交付记录](docs/0.2.0交付记录.md)：本机785通过、1权限跳过；[Windows CI](https://github.com/awa606/BiliBatchDownloader/actions/runs/39010139356)786通过、0跳过，六组窗口/缩放实际媒体检查通过。历史视频结果见[0.1.0交付记录](docs/0.1.0交付记录.md)。用户已确认r8通过本机指定目录入口显示作品、截图并正常播放。r7/r8直接启动曾读到空库：同名默认用户路径在检查与用户启动环境下对应的数据库哈希不同，原视频未丢失；改为明确的独立目录后两边一致。底层环境机制尚未判定。无Python干净Windows与完整可交互首屏≤2秒尚未验收。

用户已明确后续优先跑通功能，布局调整后置。下一模块优先接入原生图库下载，并保存/显示真实动态正文和原站标签；确认没有可留空，待获取和失败不得冒充没有。当前两个图库报告不含这些字段，详情仍显示待获取。

## 启动与使用

**本机图库入口：[启动Bili图库候选版.cmd](启动Bili图库候选版.cmd)**。它明确使用 `.private/library-gallery-verify` 和 `dist/BiliBatchDownloader-0.2.0-verify-r1`，检查库存在后以离线模式启动。候选库由旧库一致性备份建立，3作者、4作品、3视频分P、4PNG、9组实况；媒体原位引用。

原视频入口[启动Bili作品库.cmd](启动Bili作品库.cmd)继续使用r8及`.private/library`。新EXE直接启动使用独立的 `%LOCALAPPDATA%/BiliBatchDownloaderGalleryVerify`，不会迁移r8默认库。公共包为空库，无媒体、数据库或账号；直接启动空库是预期行为，本机已有作品请用上述图库入口。

0.2.0候选默认离线，添加视频入口停用，继承任务不会联网派发。新增Qt自带FFmpeg7.1.3动态运行库用于应用内预览；不捆绑外置FFmpeg/FFprobe工具或gallery-dl。视频下载代码保留，联网使用原r8已批准入口。

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

每次使用新的输出目录，保留旧失败证据。打包使用 `scripts/build_windows.py --name BiliBatchDownloader-0.2.0-新标识`；需先准备 `build/license-cache` 中完整GPLv3/LGPLv3文本，构建绝不覆盖旧目录。详见[第三方边界](docs/第三方依赖.md)。

## 项目维护

- [图库本地查看与Qt冻结验证](docs/图库本地预览验证-20261017.md)：此前隔离验证及失败证据保留；本轮人工补充和产品结果见0.2.0记录。
- [版本归档与恢复](docs/版本归档与恢复.md)、[历史版本Bug报告](docs/历史版本Bug报告.md)：r4～r8及共享材料已远端取回校验，r4～r7旧包已清理；r2/r3材料缺口保留，本机图库使用已验收的0.2.0指定入口，r8独立入口保留。
- [图库原生实况验证](docs/图库原生实况验证-20261017.md)：固定9组JPG＋MP4标准链一次通过，20请求、零重试/跳转；9视频各29～88帧，18文件SHA与完整解码通过。本机751 PASS/1权限SKIP，[Windows CI](https://github.com/awa606/BiliBatchDownloader/actions/runs/39005975713)752 PASS/0 SKIP；此报告当时尚未验收人工播放和产品图库；两项随后于0.2.0完成，GIF仍待验证。
- [图库原生四图验证](docs/图库原生四图验证-20261017.md)：此前固定4PNG标准链一次6请求通过，未在实况轮重跑。[前轮对照与候选](docs/图库原生流程验证准备.md)保留。
- [模块研究与设计评审](docs/视频作品库模块评审.md)、[完整功能与验收矩阵](docs/功能与验收矩阵.md)、[Issue #1](https://github.com/awa606/BiliBatchDownloader/issues/1)。
- [历史研究结果](docs/研究结果-20261016.md)、[历史研究工具说明](docs/历史研究工具说明-20261016.md)、[预注册](docs/研究预注册.md)。旧失败不删除；历史实况轮只执行一次固定样本；当前0.2.0轮B站请求为0。
- 自有源码 [MIT](LICENSE)。公开Git只含源码、合成测试、文档和脱敏结论；媒体、截图、数据库、浏览器档案、凭据、环境、日志和便携包均被忽略。
- 按模块研究和设计通过后编码，完成工程检查先进入VERIFY。整体研究和最终人工验收不随局部成功自动通过。

## 参考方案

[yt-dlp官方嵌入](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp)、[gallery-dl B站提取器](https://github.com/mikf/gallery-dl/blob/v1.32.16/gallery_dl/extractor/bilibili.py)、[PixivUtil2](https://github.com/Nandaka/PixivUtil2)、[Qt进程模型](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QProcess.html)。Douzy仅为用户指定的体验参考，未确认可复用桌面源码；不把其他抖音CLI项目标作Douzy。
