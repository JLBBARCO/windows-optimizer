const jsFiles = ["header.js", "footer.js", "versions.js"];

document.addEventListener("DOMContentLoaded", function () {
  jsFiles.forEach((file) => {
    const script = document.createElement("script");
    script.src = `js/${file}`;
    document.head.appendChild(script);
  });
});

function copyToClipboard() {
  const text = event.target.textContent;
  navigator.clipboard.writeText(text).then(
    () => {
      console.log("Texto copiado para a área de transferência:", text);
    },
    (err) => {
      console.error("Erro ao copiar para a área de transferência:", err);
    },
  );
}
