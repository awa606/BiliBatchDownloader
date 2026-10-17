import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia

ColumnLayout {
    id: gallery
    objectName: "galleryDetail"
    property var work: bridge.detail
    property int groupIndex: 0
    property var groups: work.groups || []
    property var group: groups.length > groupIndex ? groups[groupIndex] : ({image: {}, live_video: {}})
    property bool playingView: false
    property bool waiting: false
    property string playbackError: ""
    property string identityKey: work.work_key || ""
    readonly property int photoStatus: photo.status
    property alias mediaPlayer: player
    property alias videoOutput: video
    signal backRequested()
    spacing: 10

    function stopPreview() {
        bridge.invalidatePreview(); waiting=false; player.stop(); player.source="";
        video.clearOutput(); playingView=false; playbackError="";
    }
    function playCurrent() {
        stopPreview(); waiting=true; bridge.prepareGallery(group.sequence);
    }
    function choose(index) { stopPreview(); groupIndex=index; }
    function togglePlayback() { player.playbackState===MediaPlayer.PlayingState?player.pause():player.play(); }
    onIdentityKeyChanged: { stopPreview(); groupIndex=0; }
    Component.onDestruction: stopPreview()

    component Caption: CopyLabel { color: "#7c8395"; wrapMode: Text.Wrap }
    component Action: Button {
        id: button; implicitHeight: 36
        contentItem: Text { text: button.text; color: button.enabled?"#30374a":"#9ba1ae"; font: button.font; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
        background: Rectangle { radius: 8; color: button.hovered?"#fff0f6":"white"; border.color: "#e5e7ef" }
    }
    RowLayout {
        Layout.fillWidth: true
        Action { text: "‹ 返回作品库"; onClicked: { gallery.stopPreview(); gallery.backRequested(); } }
        Caption { Layout.fillWidth: true; horizontalAlignment: Text.AlignRight; text: "图文／图片 · "+groups.length+(work.livephoto?"组实况":"张图片") }
    }
    SelectableBody { objectName: "galleryTitle"; Layout.fillWidth: true; Layout.preferredHeight: implicitHeight; maximumTextHeight: 52; text: work.title || "读取作品…"; fontSize: 18; fontBold: true; textColor: "#232939" }
    SelectableBody { objectName: "galleryPublished"; Layout.fillWidth: true; Layout.preferredHeight: implicitHeight; fontSize: 13; textColor: "#7c8395"; text: (work.author || "作者待获取")+" · "+(work.published?new Date(work.published*1000).toLocaleString(Qt.locale("zh_CN"),"yyyy-MM-dd hh:mm:ss"):"发布时间待获取") }
    SelectableBody { objectName: "galleryTags"; Layout.fillWidth: true; Layout.preferredHeight: implicitHeight; maximumTextHeight: 52; fontSize: 13; textColor: "#7c8395"; visible: work.tags_status!=="empty"
        text: work.tags_status==="present"?"标签／话题："+(work.tags || []).concat(work.topics || []).join(" · "):
              work.tags_status==="failed"?"原站标签获取失败":"原站标签待获取" }
    SelectableBody { objectName: "galleryDescription"; Layout.fillWidth: true; Layout.preferredHeight: Math.min(85,implicitHeight)
        visible: work.description_status!=="empty"
        text: work.description || (work.description_status==="failed"?"正文获取失败":"正文待获取")
    }
    Caption { Layout.fillWidth: true; text: "已保存 "+(work.saved_groups || 0)+"/"+groups.length+" 组"+(work.selection_complete?" · 所选已完成":"") }
    Rectangle {
        Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 120
        color: "#ebeef5"; radius: 12; clip: true
        Image {
            id: photo; objectName: "galleryPhoto"; anchors.fill: parent; anchors.margins: 10
            source: gallery.group.image.preview_url || ""; visible: !gallery.playingView
            asynchronous: true; autoTransform: true; fillMode: Image.PreserveAspectFit
            sourceSize: Qt.size(1024,1024)
        }
        VideoOutput {
            id: video; objectName: "galleryVideo"; anchors.fill: parent; anchors.margins: 10
            visible: gallery.playingView; fillMode: VideoOutput.PreserveAspectFit
            endOfStreamPolicy: VideoOutput.KeepLastFrame
        }
        Caption { anchors.centerIn: parent; text: gallery.waiting?"正在核验实况文件…":gallery.playbackError || (!gallery.group.image.path?(gallery.group.selected?"本组尚未下载完成":"本组未选择保存"):"图片预览不可用，请检查文件或重新导入报告"); visible: gallery.waiting || !!gallery.playbackError || (!gallery.playingView && (photo.status===Image.Error || !gallery.group.image.path)) }
        Rectangle { anchors.left: parent.left; anchors.top: parent.top; anchors.margins: 12; radius: 6; color: "#ccffffff"; width: badge.implicitWidth+18; height: 26
            Caption { id: badge; anchors.centerIn: parent; text: gallery.playingView?"实况 · 按需播放":"原图预览"; color: "#596176" }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        Action { text: "上一组"; enabled: gallery.groupIndex>0; onClicked: gallery.choose(gallery.groupIndex-1) }
        Caption { text: (gallery.groupIndex+1)+" / "+gallery.groups.length }
        Action { text: "下一组"; enabled: gallery.groupIndex+1<gallery.groups.length; onClicked: gallery.choose(gallery.groupIndex+1) }
        Item { Layout.fillWidth: true }
        Action { objectName: "playLiveButton"; text: gallery.waiting?"核验中…":gallery.playingView?"重播实况":"播放实况"; visible: !!gallery.group.live_video.path; enabled: !gallery.waiting; onClicked: gallery.playCurrent() }
        Action { text: player.playbackState===MediaPlayer.PlayingState?"暂停":"继续"; visible: gallery.playingView && player.mediaStatus!==MediaPlayer.EndOfMedia; onClicked: gallery.togglePlayback() }
        Action { text: "切回照片"; visible: gallery.playingView || gallery.waiting; onClicked: gallery.stopPreview() }
    }
    ListView {
        Layout.fillWidth: true; Layout.preferredHeight: 76; orientation: ListView.Horizontal; spacing: 8; clip: true
        model: gallery.groups; currentIndex: gallery.groupIndex
        onCurrentIndexChanged: positionViewAtIndex(currentIndex,ListView.Contain)
        delegate: Rectangle {
            required property var modelData; required property int index
            width: 86; height: 70; radius: 7; color: "white"; border.width: 2; border.color: gallery.groupIndex===index?"#ed6495":"#e3e6ef"
            Image { anchors.fill: parent; anchors.margins: 5; source: modelData.image.preview_url || ""; asynchronous: true; autoTransform: true; sourceSize: Qt.size(160,120); fillMode: Image.PreserveAspectFit }
            Rectangle { anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter; width: 60; height: 18; color: "#cfffffff"
                Caption { anchors.centerIn: parent; font.pixelSize: 10; text: modelData.sequence+(modelData.live_video.path?" · 实况":" · 图片") }
            }
            MouseArea { anchors.fill: parent; onClicked: gallery.choose(index) }
        }
        ScrollBar.horizontal: ScrollBar {}
    }
    RowLayout {
        Layout.fillWidth: true
        Action { text: "打开原图"; enabled: !!gallery.group.image.path; onClicked: bridge.openGallery(gallery.group.sequence,"image",false) }
        Action { text: "默认播放器播放"; visible: !!gallery.group.live_video.path; onClicked: bridge.openGallery(gallery.group.sequence,"live_video",false) }
        Action { text: "打开文件夹"; enabled: !!gallery.group.image.path; onClicked: bridge.openGallery(gallery.group.sequence,"image",true) }
        Item { Layout.fillWidth: true }
        Caption { text: work.livephoto?"实况配套视频 · 无音轨属正常":"原文件原位保留"; font.pixelSize: 11 }
    }
    MediaPlayer {
        id: player; objectName: "galleryPlayer"; videoOutput: video; audioOutput: AudioOutput { muted: true }
        loops: 1
        onErrorOccurred: function(error, ignoredText) { gallery.playbackError="实况播放失败，原文件已保留；请检查文件或使用默认播放器。"; player.stop(); player.source=""; video.clearOutput(); gallery.playingView=false; }
    }
    Connections {
        target: bridge
        function onPreviewReady(url) {
            if (!gallery.waiting) return;
            gallery.waiting=false;
            if (!url) { gallery.playbackError="文件核验失败，未开始播放。"; return; }
            gallery.playingView=true; player.source=url; player.play();
        }
    }
}
