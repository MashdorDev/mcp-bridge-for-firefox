/* globals ExtensionAPI, ExtensionCommon, Services */
"use strict";

const { ExtensionError } = ExtensionCommon;

function mainWindow() {
  const win = Services.wm.getMostRecentWindow("mail:3pane");
  if (!win) throw new ExtensionError("No Thunderbird main window is open");
  return win;
}

var mcpChrome = class extends ExtensionAPI {
  getAPI() {
    return {
      mcpChrome: {
        async captureWindow(scale) {
          const win = mainWindow();
          // drawSnapshot composites the chrome and every tab's content, which
          // tabs.captureVisibleTab cannot: the mail view is not a content tab.
          const rect = new win.DOMRect(0, 0, win.innerWidth, win.innerHeight);
          const bitmap = await win.browsingContext.currentWindowGlobal.drawSnapshot(rect, scale, "white");
          const canvas = win.document.createElementNS("http://www.w3.org/1999/xhtml", "canvas");
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
          canvas.getContext("2d").drawImage(bitmap, 0, 0);
          bitmap.close();
          return { dataUrl: canvas.toDataURL("image/png"), width: canvas.width, height: canvas.height };
        },

        // Pairs without copying the token by hand. Only this one file, at a
        // fixed path (PAIRING_TOKEN_FILE in server.py), is ever read.
        async readPairingToken() {
          const path = PathUtils.join(Services.dirsvc.get("Home", Ci.nsIFile).path, ".foxmcp", "pairing-token");
          try {
            return (await IOUtils.readUTF8(path)).trim();
          } catch (error) {
            if (DOMException.isInstance(error) && error.name === "NotFoundError") return null;
            throw new ExtensionError(`Could not read ${path}: ${error.message}`);
          }
        },
      },
    };
  }
};
