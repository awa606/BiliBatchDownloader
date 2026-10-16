import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: win
    width: 1240; height: 820; minimumWidth: 940; minimumHeight: 620
    visible: true; title: "BiliBatchDownloader · 作者作品库 0.1.0 VERIFY"
    color: "#f7f8fc"
    property string pageName: "library"
    property string selectedUid: ""
    property string groupUid: ""
    property var selectedParts: ({})
    readonly property color pink: "#ed6495"
    readonly property color ink: "#232939"
    readonly property color muted: "#7c8395"
    font.family: "Microsoft YaHei UI"; font.pixelSize: 13

    function stamp(seconds) { var n=Math.floor(seconds); return Math.floor(n/60)+":"+(n%60<10?"0":"")+(n%60); }
    function taskState(value) { return ({queued:"等待派发",running:"进行中",complete:"已完成",failed:"失败",canceled:"已取消",interrupted:"已中断"})[value] || value; }
    component AppButton: Button {
        id: control
        property bool accent: false
        implicitHeight: 38; implicitWidth: Math.max(80,contentItem.implicitWidth+28)
        opacity: enabled ? 1 : .45
        contentItem: Text { text: control.text; color: control.accent ? "white" : win.ink; font: control.font; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
        background: Rectangle { radius: 9; color: control.accent ? (control.hovered ? "#df5286" : win.pink) : (control.hovered ? "#f0f1f7" : "white"); border.color: control.accent ? "transparent" : "#e5e7ef" }
    }
    component Field: TextField {
        id: field; implicitHeight: 40; color: win.ink; selectByMouse: true
        leftPadding: 12; rightPadding: 12
        background: Rectangle { color: "white"; radius: 9; border.color: field.activeFocus ? win.pink : "#e3e6ef" }
    }
    component Hint: SafeLabel { color: win.muted; wrapMode: Text.Wrap; font.pixelSize: 12 }
    component SafeLabel: Label { textFormat: Text.PlainText }
    component Selector: ComboBox {
        id: selector; implicitHeight: 40
        background: Rectangle { radius: 9; color: "white"; border.color: "#e3e6ef" }
        contentItem: Text { text: selector.displayText; textFormat: Text.PlainText; color: win.ink; font: selector.font; verticalAlignment: Text.AlignVCenter; leftPadding: 12; rightPadding: 30; elide: Text.ElideRight }
        indicator: Text { anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter; text: "⌄"; color: win.muted; font.pixelSize: 18 }
    }

    RowLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            Layout.preferredWidth: 230; Layout.fillHeight: true; color: "white"
            Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: "#e9ebf2" }
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 20; spacing: 12
                RowLayout {
                    spacing: 10; Layout.topMargin: 10; Layout.bottomMargin: 17
                    Rectangle { width: 38; height: 38; radius: 11; color: win.pink
                        SafeLabel { anchors.centerIn: parent; text: "b"; color: "white"; font.pixelSize: 28; font.bold: true }
                    }
                    Column { spacing: 3
                        SafeLabel { text: "Bili 作品库"; color: win.ink; font.pixelSize: 19; font.bold: true }
                        Hint { text: "BATCH DOWNLOADER"; font.pixelSize: 9; font.letterSpacing: 1 }
                    }
                }
                AppButton { text: "▦   我的作品库"; Layout.fillWidth: true; accent: win.pageName!=="tasks"; onClicked: { win.pageName="library"; bridge.refresh(); } }
                AppButton { text: "↓   下载中心"; Layout.fillWidth: true; accent: win.pageName==="tasks"; onClicked: { win.pageName="tasks"; bridge.refresh(); } }
                RowLayout { Layout.topMargin: 20
                    Hint { text: "本地作者"; Layout.fillWidth: true; font.bold: true }
                    Hint { text: bridge.authors.length+" 位" }
                }
                Selector { id: groupFilter; Layout.fillWidth: true; model: {
                    var groups=["全部分组"]; for (var i=0;i<bridge.authors.length;i++) if (groups.indexOf(bridge.authors[i].group_name)<0) groups.push(bridge.authors[i].group_name); return groups;
                } }
                AppButton { text: "全部作者"; Layout.fillWidth: true; onClicked: { win.selectedUid=""; bridge.selectAuthor(""); win.pageName="library"; } }
                ListView {
                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: 6; clip: true
                    model: bridge.authors.filter(function(a){ return groupFilter.currentText==="全部分组" || a.group_name===groupFilter.currentText; })
                    delegate: Rectangle {
                        required property var modelData
                        width: ListView.view.width; height: 68; radius: 10
                        color: win.selectedUid===modelData.uid ? "#fff0f6" : (authorMouse.containsMouse?"#f8f9fc":"white")
                        RowLayout { anchors.fill: parent; anchors.margins: 8; spacing: 9
                            Rectangle { width: 34; height: 34; radius: 17; color: "#f1e7f5"
                                SafeLabel { anchors.centerIn: parent; text: String(modelData.name).charAt(0); color: "#ad73a6"; font.bold: true }
                            }
                            ColumnLayout { spacing: 5; Layout.fillWidth: true
                                SafeLabel { text: modelData.name; Layout.fillWidth: true; elide: Text.ElideRight; color: win.ink; font.pixelSize: 12 }
                                Hint { text: modelData.count+" 部作品 · "+modelData.group_name; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 10 }
                            }
                        }
                        MouseArea { id: authorMouse; anchors.fill: parent; hoverEnabled: true; onClicked: { win.selectedUid=modelData.uid; bridge.selectAuthor(modelData.uid); win.pageName="library"; } }
                    }
                    ScrollBar.vertical: ScrollBar {}
                }
                Hint { text: "仅显示已登记的作者与作品。\n关注同步将在后续模块提供。"; Layout.fillWidth: true; font.pixelSize: 11 }
                AppButton { text: "⚙   设置与导入"; Layout.fillWidth: true; onClicked: settingsDialog.open() }
                Hint { text: "0.1.0  ·  VERIFY 候选版"; Layout.topMargin: 6; font.pixelSize: 10 }
            }
        }
        ColumnLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.margins: 28; spacing: 18
            RowLayout {
                Layout.fillWidth: true
                ColumnLayout { spacing: 5; Layout.fillWidth: true
                    SafeLabel { text: win.pageName==="tasks" ? "下载中心" : win.pageName==="detail" ? "作品详情" : "我的作品库"; color: win.ink; font.pixelSize: 27; font.bold: true }
                    Hint { text: win.pageName==="tasks" ? "后台串行处理 · 原文件与中断文件保留" : "按作者收藏，让每一部作品都有自己的位置" }
                }
                Rectangle { width: statusText.implicitWidth+22; height: 28; radius: 14; color: "#e9f6f1"
                    SafeLabel { id: statusText; anchors.centerIn: parent; text: bridge.online?"● 原生视频已验证":"● 离线作品库"; color: "#468c73"; font.pixelSize: 11 }
                }
                AppButton { text: "+ 添加视频"; accent: true; onClicked: addDialog.open() }
            }

            Loader {
                Layout.fillWidth: true; Layout.fillHeight: true
                sourceComponent: win.pageName==="tasks" ? tasksPage : win.pageName==="detail" ? detailPage : libraryPage
            }
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: infoText.implicitHeight+22; radius: 9; color: "#edf0f7"
                SafeLabel { id: infoText; anchors.fill: parent; anchors.margins: 11; color: "#657089"; font.pixelSize: 11; wrapMode: Text.Wrap
                    text: bridge.message || "本地作品离线可用 · 缺少来源依据的作品保留为待分类 · 未下载作品不生成虚假预览"
                }
            }
        }
    }

    Component {
        id: libraryPage
        ColumnLayout {
            spacing: 15
            RowLayout {
                Layout.fillWidth: true
                SafeLabel { text: bridge.authorLabel; color: win.ink; font.bold: true; font.pixelSize: 16; elide: Text.ElideRight; Layout.maximumWidth: 300 }
                Hint { text: bridge.total+" 部作品"; Layout.fillWidth: true }
                AppButton { visible: win.selectedUid!==""; text: "设置作者分组"; onClicked: { win.groupUid=win.selectedUid; groupDialog.open(); } }
            }
            RowLayout {
                Layout.fillWidth: true; spacing: 10
                Field { id: searchBox; text: bridge.searchText; Layout.fillWidth: true; placeholderText: "搜索作者、标题、正文、原站标签"; onTextEdited: searchDelay.restart()
                    Timer { id: searchDelay; interval: 250; onTriggered: bridge.search(searchBox.text) }
                }
                Selector { model: ["全部分类","图文／图片","投稿视频","动态视频","待分类"]; currentIndex: Math.max(0,model.indexOf(bridge.categoryFilter)); onActivated: bridge.category(currentIndex===0?"":currentText); implicitWidth: 132 }
                CheckBox { text: "仅未下载"; checked: bridge.missingFilter; onToggled: bridge.onlyMissing(checked) }
            }
            GridView {
                id: grid; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                property int columns: Math.max(2,Math.floor(width/270))
                cellWidth: width/columns; cellHeight: 254; model: bridge.works
                delegate: Item {
                    required property var modelData
                    width: grid.cellWidth; height: grid.cellHeight
                    Rectangle {
                        anchors.fill: parent; anchors.rightMargin: 14; anchors.bottomMargin: 14; radius: 13; color: "white"; border.color: cardMouse.containsMouse?"#f2b6ce":"#e8eaf1"
                        ColumnLayout {
                            anchors.fill: parent; anchors.margins: 10; spacing: 9
                            Rectangle {
                                Layout.fillWidth: true; Layout.preferredHeight: 138; radius: 8; color: "#eef0f6"; clip: true
                                Image { anchors.fill: parent; source: modelData.cover || ""; asynchronous: true; fillMode: Image.PreserveAspectCrop; sourceSize.width: 640 }
                                SafeLabel { anchors.centerIn: parent; visible: !modelData.cover; text: "▷\n下载后生成预览"; horizontalAlignment: Text.AlignHCenter; color: "#a0a7b8"; font.pixelSize: 15 }
                                Rectangle { anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 8; width: pill.implicitWidth+16; height: 23; radius: 6; color: "#cc273044"
                                    SafeLabel { id: pill; anchors.centerIn: parent; text: modelData.local_count+" / "+modelData.parts+" P 已下载"; color: "white"; font.pixelSize: 10 }
                                }
                                Rectangle { visible: !!modelData.cover; anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.margins: 7; width: 78; height: 21; radius: 5; color: "#b3232939"
                                    SafeLabel { anchors.centerIn: parent; text: "实际内容截图"; color: "white"; font.pixelSize: 10 }
                                }
                            }
                            SafeLabel { text: modelData.title; color: win.ink; font.bold: true; Layout.fillWidth: true; maximumLineCount: 2; wrapMode: Text.Wrap; elide: Text.ElideRight }
                            RowLayout { Layout.fillWidth: true
                                Hint { text: modelData.author; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 11 }
                                Hint { text: modelData.category; color: "#c87898"; font.pixelSize: 10 }
                            }
                            Item { Layout.fillHeight: true }
                        }
                        MouseArea { id: cardMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { bridge.openWork(modelData.bvid); win.selectedParts=({}); win.pageName="detail"; } }
                    }
                }
                SafeLabel { anchors.centerIn: parent; visible: grid.count===0; text: "这里还没有作品\n在设置中导入已核验的本地报告，或添加视频"; color: win.muted; horizontalAlignment: Text.AlignHCenter; lineHeight: 1.7 }
                ScrollBar.vertical: ScrollBar {}
            }
            RowLayout { Layout.fillWidth: true
                Hint { text: "每页最多 40 部 · 图片与动态下载尚未开放"; Layout.fillWidth: true }
                AppButton { text: "上一页"; enabled: bridge.offset>0; onClicked: bridge.page(-1) }
                Hint { text: Math.floor(bridge.offset/40)+1+" / "+Math.max(1,Math.ceil(bridge.total/40)) }
                AppButton { text: "下一页"; enabled: bridge.offset+40<bridge.total; onClicked: bridge.page(1) }
            }
        }
    }

    Component {
        id: detailPage
        ColumnLayout {
            spacing: 14
            RowLayout {
                Layout.fillWidth: true
                AppButton { text: "‹ 返回作品库"; onClicked: win.pageName="library" }
                Item { Layout.fillWidth: true }
                Hint { text: "本地分类" }
                Selector { id: classifyBox; model: ["图文／图片","投稿视频","动态视频","待分类"]; currentIndex: Math.max(0,model.indexOf(bridge.detail.category || "待分类")); onActivated: bridge.classify(currentText); implicitWidth: 132 }
            }
            ScrollView {
                id: detailScroll; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
                ColumnLayout {
                    width: detailScroll.availableWidth; spacing: 18
                    Rectangle {
                        Layout.fillWidth: true; implicitHeight: summary.implicitHeight+36; radius: 13; color: "white"; border.color: "#e8eaf1"
                        ColumnLayout { id: summary; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 18; spacing: 12
                            SafeLabel { text: bridge.detail.title || "读取作品…"; font.pixelSize: 22; font.bold: true; color: win.ink; wrapMode: Text.Wrap; Layout.fillWidth: true }
                            Hint { text: (bridge.detail.author || "作者待获取")+"   ·   "+(bridge.detail.bvid || "")+"   ·   "+(bridge.detail.category || "待分类") }
                            Hint { text: bridge.detail.published ? new Date(bridge.detail.published*1000).toLocaleDateString(Qt.locale("zh_CN")) : "发布时间待获取" }
                            SafeLabel { text: bridge.detail.description || "简介待获取"; color: "#697287"; Layout.fillWidth: true; wrapMode: Text.Wrap; maximumLineCount: 8; elide: Text.ElideRight }
                            SafeLabel { text: (bridge.detail.tags || []).length ? "原站标签："+bridge.detail.tags.join("  ·  ") : ({pending:"原站标签：待获取",empty:"原站确实无标签",failed:"原站标签获取失败"})[bridge.detail.tags_status] || "原站标签待获取"; wrapMode: Text.Wrap; Layout.fillWidth: true; color: "#bc7290"; font.pixelSize: 12 }
                            Hint { text: bridge.detail.category_evidence==="user_correction"?"分类依据：用户手动纠正（不会移动原文件）":"分类依据不足，保留待分类；BV号和文件格式不能证明投稿来源。"; Layout.fillWidth: true }
                            RowLayout {
                                visible: (bridge.detail.assets || []).length>0 && bridge.detail.assets[0].status==="complete"
                                AppButton { text: "▷ 播放首个分P"; accent: true; onClicked: bridge.openLocal(bridge.detail.assets[0].cid,false) }
                                AppButton { text: "打开文件夹"; onClicked: bridge.openLocal(bridge.detail.assets[0].cid,true) }
                            }
                        }
                    }
                    RowLayout { Layout.fillWidth: true
                        SafeLabel { text: "分P与内容预览"; color: win.ink; font.bold: true; font.pixelSize: 17; Layout.fillWidth: true }
                        AppButton { text: "下载选中分P"; enabled: bridge.online; onClicked: { for (var key in win.selectedParts) if(win.selectedParts[key]) bridge.download(key); } }
                    }
                    Repeater {
                        model: bridge.detail.assets || []
                        delegate: Rectangle {
                            required property var modelData
                            Layout.fillWidth: true; implicitHeight: partBody.implicitHeight+30; radius: 12; color: "white"; border.color: "#e8eaf1"
                            ColumnLayout {
                                id: partBody; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 15; spacing: 12
                                RowLayout { Layout.fillWidth: true
                                    CheckBox { text: "P"+modelData.part; enabled: modelData.status!=="complete" && bridge.online; onToggled: { var parts=win.selectedParts; parts[modelData.cid]=checked; win.selectedParts=parts; } }
                                    SafeLabel { text: modelData.title || "分P标题待获取"; color: win.ink; Layout.fillWidth: true; elide: Text.ElideRight }
                                    Hint { text: win.stamp(modelData.duration)+" · CID "+modelData.cid }
                                }
                                GridLayout { columns: detailScroll.width>900 ? 3 : 2; columnSpacing: 10; rowSpacing: 10; Layout.fillWidth: true
                                    Repeater { model: modelData.previews
                                        delegate: ColumnLayout {
                                            required property var modelData
                                            Layout.fillWidth: true; spacing: 5
                                            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: Math.max(100,(detailScroll.width-70)/ (detailScroll.width>900?3:2)*.56); radius: 7; color: "#eef0f6"; clip: true
                                                Image { anchors.fill: parent; source: modelData.url; fillMode: Image.PreserveAspectFit; asynchronous: true; sourceSize.width: 640 }
                                            }
                                            Hint { text: "实际帧时间  "+win.stamp(modelData.seconds); font.pixelSize: 11 }
                                        }
                                    }
                                }
                                Hint { visible: !modelData.previews.length; text: modelData.status==="complete"?"尚无内容截图；视频文件状态独立记录。":"下载后生成内容截图" }
                                RowLayout { Layout.fillWidth: true
                                    Hint { text: modelData.status==="complete"?"● 已核验本地文件 · "+(modelData.size/1048576).toFixed(1)+" MiB":"○ 未下载"; Layout.fillWidth: true; color: modelData.status==="complete"?"#468c73":win.muted }
                                    AppButton { text: "打开文件夹"; visible: modelData.status==="complete"; onClicked: bridge.openLocal(modelData.cid,true) }
                                    AppButton { text: modelData.status==="complete"?"▷ 播放视频":"↓ 下载本P"; accent: true; enabled: modelData.status==="complete" || bridge.online; onClicked: modelData.status==="complete"?bridge.openLocal(modelData.cid,false):bridge.download(modelData.cid) }
                                }
                                Hint { text: modelData.path || ""; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; font.pixelSize: 10 }
                            }
                        }
                    }
                }
            }
        }
    }

    Component {
        id: tasksPage
        ColumnLayout {
            RowLayout { Layout.fillWidth: true
                Hint { text: bridge.paused?"派发已暂停，当前任务不受影响":"按加入顺序逐项执行"; Layout.fillWidth: true }
                AppButton { text: bridge.paused?"继续派发":"暂停派发"; accent: bridge.paused; onClicked: bridge.togglePause() }
            }
            ListView {
                id: taskList; Layout.fillWidth: true; Layout.fillHeight: true; model: bridge.tasks; clip: true; spacing: 12
                delegate: Rectangle {
                    required property var modelData
                    width: taskList.width; height: 130; radius: 12; color: "white"; border.color: "#e8eaf1"
                    ColumnLayout { anchors.fill: parent; anchors.margins: 16; spacing: 9
                        RowLayout { Layout.fillWidth: true
                            SafeLabel { text: ({import:"登记本地作品",check_file:"打开本地文件",metadata:"读取视频信息",download:"下载视频"})[modelData.kind] || "后台任务"; color: win.ink; font.bold: true }
                            Hint { text: modelData.key.startsWith("import:")?"原位登记":modelData.key.replace(/^(metadata|download|open):/,""); Layout.fillWidth: true; elide: Text.ElideMiddle }
                            SafeLabel { text: win.taskState(modelData.state); color: modelData.state==="failed"?"#d96d71":"#649482"; font.pixelSize: 12 }
                        }
                        ProgressBar { Layout.fillWidth: true; from: 0; to: 100; value: modelData.progress }
                        RowLayout { Layout.fillWidth: true
                            Hint { text: modelData.phase+(modelData.speed>0?" · "+(modelData.speed/1048576).toFixed(2)+" MiB/s":"")+(modelData.error?" · "+modelData.error:""); Layout.fillWidth: true; elide: Text.ElideRight }
                            AppButton { text: "取消"; visible: modelData.state==="queued" || modelData.state==="running"; onClicked: bridge.cancel(modelData.id) }
                            AppButton { text: "重新加入"; visible: ["failed","canceled","interrupted"].indexOf(modelData.state)>=0; onClicked: bridge.retry(modelData.id) }
                        }
                    }
                }
                SafeLabel { anchors.centerIn: parent; visible: taskList.count===0; text: "暂无任务\n添加视频不会自动下载全部分P"; color: win.muted; horizontalAlignment: Text.AlignHCenter; lineHeight: 1.7 }
                ScrollBar.vertical: ScrollBar {}
            }
        }
    }

    Dialog {
        id: addDialog; anchors.centerIn: parent; width: 570; modal: true; title: "添加视频到作品库"; standardButtons: Dialog.Close
        ColumnLayout { width: parent.width; spacing: 15
            Hint { text: bridge.online?"读取信息后，在作品详情选择需要下载的分P。当前验证范围最高360p。":"原生下载尚未通过本轮验证；当前版本可导入并浏览本地作品。"; Layout.fillWidth: true }
            Field { id: videoInput; Layout.fillWidth: true; placeholderText: "BV号 / https://www.bilibili.com/video/…"; onTextEdited: inputHint.text="输入已改变，请重新读取。" }
            RowLayout { SafeLabel { text: "读取分P" } SpinBox { id: partInput; from: 1; to: 1000; editable: true; onValueModified: inputHint.text="分P已改变，请重新读取。" }
                Item { Layout.fillWidth: true }
                AppButton { text: "读取作品信息"; accent: true; enabled: bridge.online; onClicked: { bridge.addVideo(videoInput.text,partInput.value); addDialog.close(); win.pageName="tasks"; } }
            }
            Hint { id: inputHint; text: "不会自动下载全部作品或关注列表。"; Layout.fillWidth: true }
        }
    }
    Dialog {
        id: groupDialog; anchors.centerIn: parent; width: 380; modal: true; title: "作者本地分组"; standardButtons: Dialog.Ok | Dialog.Cancel
        Field { id: groupInput; width: parent.width; placeholderText: "输入分组名称" }
        onAccepted: bridge.groupAuthor(win.groupUid,groupInput.text)
    }
    Dialog {
        id: settingsDialog; anchors.centerIn: parent; width: Math.min(760,win.width-60); modal: true; title: "设置与本地报告导入"; standardButtons: Dialog.Close
        onOpened: { profileInput.text=bridge.settings.profile; rootInput.text=bridge.settings.root; ffmpegInput.text=bridge.settings.ffmpeg; }
        ColumnLayout { width: parent.width; spacing: 12
            Hint { text: "独立用户数据目录 · 不保存 Cookie · 不改系统播放器关联"; Layout.fillWidth: true }
            SafeLabel { text: "专用 Chrome 档案目录（先在旧工具完成登录，再关闭该浏览器）"; color: win.ink }
            Field { id: profileInput; Layout.fillWidth: true; placeholderText: "选择你已登录B站的专用档案目录" }
            RowLayout { Layout.fillWidth: true
                Field { id: rootInput; Layout.fillWidth: true; placeholderText: "新下载保存目录" }
                AppButton { text: "选择目录"; onClicked: folderPicker.open() }
            }
            RowLayout { Layout.fillWidth: true
                Field { id: ffmpegInput; Layout.fillWidth: true; placeholderText: "FFmpeg完整路径，旁边需有FFprobe" }
                AppButton { text: "选择工具"; onClicked: toolPicker.open() }
            }
            AppButton { text: "保存设置"; accent: true; onClicked: bridge.saveSettings(profileInput.text,rootInput.text,ffmpegInput.text) }
            Rectangle { Layout.fillWidth: true; height: 1; color: "#e5e7ef"; Layout.topMargin: 6 }
            SafeLabel { text: "导入已核验研究报告"; color: win.ink; font.bold: true }
            Hint { text: "此入口只登记本工具已核验的视频与预览报告。会复核每个文件的SHA-256，原文件原位保留；通用目录预扫描导入将在后续版本提供。"; Layout.fillWidth: true }
            RowLayout { Layout.fillWidth: true
                Field { id: sourceInput; Layout.fillWidth: true; placeholderText: "视频下载 report.json" }
                AppButton { text: "选择报告"; onClicked: sourcePicker.open() }
            }
            RowLayout { Layout.fillWidth: true
                Field { id: previewInput; Layout.fillWidth: true; placeholderText: "本地预览 report.json" }
                AppButton { text: "选择预览"; onClicked: previewPicker.open() }
            }
            AppButton { text: "核验并原位登记"; onClicked: { bridge.importReports(sourceInput.text,previewInput.text); settingsDialog.close(); win.pageName="tasks"; } }
        }
    }
    FolderDialog { id: folderPicker; onAccepted: rootInput.text=selectedFolder.toString() }
    FileDialog { id: toolPicker; nameFilters: ["FFmpeg (ffmpeg.exe)"]; onAccepted: ffmpegInput.text=selectedFile.toString() }
    FileDialog { id: sourcePicker; nameFilters: ["报告 (*.json)"]; onAccepted: sourceInput.text=selectedFile.toString() }
    FileDialog { id: previewPicker; nameFilters: ["报告 (*.json)"]; onAccepted: previewInput.text=selectedFile.toString() }
}
