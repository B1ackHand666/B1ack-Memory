(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const React = SDK.React;
  const useEffect = SDK.hooks.useEffect;
  const useState = SDK.hooks.useState;
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

  function escapeInlineScript(source) {
    return String(source).replace(/<\/script/gi, "<\\/script");
  }

  function embeddedDocument(bundle) {
    return String(bundle.html)
      .replace(/<link[^>]+href=["']style\.css["'][^>]*>/i, "<style>" + bundle.css + "</style>")
      .replace(
        /<script[^>]+src=["']app\.js["'][^>]*><\/script>/i,
        "<script>window.__B1ACK_MEMORY_EMBEDDED__=true;</script>" +
          "<script>" + escapeInlineScript(bundle.js) + "</script>",
      );
  }

  function B1ackMemoryPage() {
    const documentState = useState(null);
    const documentHtml = documentState[0];
    const setDocumentHtml = documentState[1];
    const errorState = useState("");
    const error = errorState[0];
    const setError = errorState[1];

    useEffect(function () {
      let cancelled = false;
      SDK.fetchJSON(API + "/ui-bundle")
        .then(function (bundle) {
          if (!cancelled) setDocumentHtml(embeddedDocument(bundle));
        })
        .catch(function (reason) {
          if (!cancelled) setError(String(reason && reason.message ? reason.message : reason));
        });
      return function () {
        cancelled = true;
      };
    }, []);

    if (error) {
      return React.createElement(
        "div",
        { style: { padding: "1rem", color: "#ff8b8b" } },
        "B1ack Memory 加载失败：" + error,
      );
    }
    if (!documentHtml) {
      return React.createElement(
        "div",
        { style: { padding: "1rem", color: "var(--color-muted-foreground, #9ca3af)" } },
        "正在加载 B1ack Memory…",
      );
    }
    return React.createElement("iframe", {
      srcDoc: documentHtml,
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
