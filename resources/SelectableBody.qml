import QtQuick
import QtQuick.Controls

// Native read-only selection and clipboard support, with no rich-text parsing.
ScrollView {
    id: view
    property alias text: body.text
    property alias textColor: body.color
    property alias fontSize: body.font.pixelSize
    property alias fontBold: body.font.bold
    property real maximumTextHeight: 100
    property alias editor: body
    implicitHeight: Math.min(maximumTextHeight, body.implicitHeight)
    contentWidth: availableWidth
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    clip: true
    TextArea {
        id: body
        objectName: "selectableBodyEditor"
        width: view.availableWidth
        readOnly: true
        selectByMouse: true
        persistentSelection: true
        textFormat: TextEdit.PlainText
        wrapMode: TextEdit.Wrap
        color: "#697287"
        font.pixelSize: 14
        padding: 0
        background: null
        function copyWholeText() { selectAll(); copy(); }
        TapHandler {
            acceptedButtons: Qt.RightButton
            onTapped: menu.popup()
        }
        Menu {
            id: menu
            MenuItem { text: "复制所选文字"; enabled: body.selectedText.length>0; onTriggered: body.copy() }
            MenuItem { text: "全选"; onTriggered: body.selectAll() }
            MenuItem { text: "复制全文"; onTriggered: body.copyWholeText() }
        }
    }
}
