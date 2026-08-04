(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const React = SDK.React;
  const dashboardBasePath = (window.__HERMES_BASE_PATH__ || "").replace(/\/$/, "");

  function B1ackMemoryPage() {
    return React.createElement("iframe", {
      src: dashboardBasePath + "/api/plugins/b1ack-memory/ui/",
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
