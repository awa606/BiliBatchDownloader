import QtQuick
import QtQuick.Controls

// Keep Label sizing/elision and left-click navigation; copy the unelided text.
Label {
    id: label
    textFormat: Text.PlainText
    function copyWholeText() { bridge.copyText(label.text); }
    TapHandler {
        acceptedButtons: Qt.RightButton
        onTapped: copyMenu.popup()
    }
    Menu {
        id: copyMenu
        objectName: "copyTextMenu"
        popupType: Popup.Item
        MenuItem {
            objectName: "copyLabelAction"
            text: "复制文字"
            enabled: label.text.length > 0
            onTriggered: label.copyWholeText()
        }
    }
}
