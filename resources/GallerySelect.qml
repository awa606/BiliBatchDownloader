import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: selection
    objectName: "gallerySelection"
    property var draft: bridge.galleryDraft
    property var chosen: ({})
    property string draftId: draft.draft_id || ""
    signal submitted()
    onDraftIdChanged: chosen=({})
    spacing: 10
    Label { text: "选择要保存的图片／实况组"; font.pixelSize: 21; font.bold: true; color: "#30374a" }
    Label { Layout.fillWidth: true; text: (selection.draft.metadata || {}).author || ""; textFormat: Text.PlainText }
    SelectableBody {
        objectName: "selectionDescription"
        Layout.fillWidth: true; Layout.preferredHeight: Math.min(90,implicitHeight)
        visible: text.length>0
        text: ((selection.draft.metadata || {}).description || "")+((selection.draft.metadata || {}).tags_status==="present"?
            "\n标签／话题："+((selection.draft.metadata.tags || []).concat(selection.draft.metadata.topics || [])).join(" · "):"")
    }
    Label { Layout.fillWidth: true; wrapMode: Text.Wrap; color: "#778094"
        text: "预览已读取照片并临时缓存；仅勾选内容进入归档，实况视频在确认后下载。点击图片可放大。" }
    GridView {
        id: grid; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
        cellWidth: 190; cellHeight: 185; model: selection.draft.groups || []
        delegate: Rectangle {
            required property var modelData
            width: 180; height: 175; radius: 9; color: "white"; border.color: "#e5e7ef"
            ColumnLayout { anchors.fill: parent; anchors.margins: 7
                Image { Layout.fillWidth: true; Layout.fillHeight: true; source: modelData.preview_url
                    sourceSize: Qt.size(360,260); asynchronous: true; fillMode: Image.PreserveAspectFit
                    MouseArea { anchors.fill: parent; onClicked: { enlarged.source=modelData.preview_url; photoDialog.open(); } }
                }
                CheckBox { objectName: "galleryChoice"+modelData.sequence
                    text: "第"+modelData.sequence+"组"+(modelData.livephoto?" · 实况":" · 图片")
                    checked: !!selection.chosen[modelData.sequence]
                    onToggled: { var next=Object.assign({},selection.chosen); if(checked) next[modelData.sequence]=true;
                        else delete next[modelData.sequence]; selection.chosen=next; }
                }
            }
        }
        ScrollBar.vertical: ScrollBar {}
    }
    RowLayout {
        Label { text: "已选 "+Object.keys(selection.chosen).length+" / "+(selection.draft.groups || []).length+" 组"; Layout.fillWidth: true }
        Button { text: "清空选择"; onClicked: selection.chosen=({}) }
        Button { objectName: "downloadGallerySelection"; text: "下载所选组"; enabled: Object.keys(selection.chosen).length>0 && bridge.galleryOnline
            onClicked: { bridge.downloadGallery(JSON.stringify(Object.keys(selection.chosen).map(Number).sort(function(a,b){return a-b;}))); selection.submitted(); } }
    }
    Dialog { id: photoDialog; anchors.centerIn: parent; width: Math.min(800,selection.width); height: Math.min(650,selection.height)
        modal: true; standardButtons: Dialog.Close
        Image { id: enlarged; anchors.fill: parent; asynchronous: true; fillMode: Image.PreserveAspectFit; sourceSize: Qt.size(1024,1024) }
    }
}
