(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const React = SDK.React;
  const API = "/api/plugins/b1ack-memory";

  window.__B1ACK_MEMORY_DASHBOARD_BRIDGE__ = {
    request: function (path, options) {
      return SDK.fetchJSON(API + path, options || {});
    },
    exportText: function () {
      if (!SDK.authedFetch) {
        return Promise.reject(new Error("当前 Hermes Dashboard 不支持认证下载"));
      }
      return SDK.authedFetch(API + "/export").then(function (response) {
        if (!response.ok) {
          return response.text().then(function (body) {
            throw new Error(body || response.statusText);
          });
        }
        return response.text();
      });
    },
  };

  function B1ackMemoryPage() {
    return React.createElement("iframe", {
      src: API + "/ui/",
      title: "B1ack Memory",
      style: {
        border: 0,
        width: "100%",
        minHeight: "calc(100vh - 7rem)",
        background: "#0b0e14",
      },
    });
  }

  window.__HERMES_PLUGINS__.register("b1ack-memory", B1ackMemoryPage);
})();
