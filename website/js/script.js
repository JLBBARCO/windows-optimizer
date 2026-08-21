const nav = document.querySelector(".site-nav");
const sections = [...document.querySelectorAll("main section[id]")];
const toast = document.querySelector(".toast");

const labels = {
  home: "Visão geral",
  commands: "Comandos",
  features: "Funcionalidades",
  versions: "Releases",
};

const navList = document.createElement("ul");
sections.forEach((section) => {
  const link = document.createElement("a");
  link.href = `#${section.id}`;
  link.textContent = labels[section.id] || section.getAttribute("aria-label");
  const item = document.createElement("li");
  item.appendChild(link);
  navList.appendChild(item);
});
nav.appendChild(navList);

const activateNav = () => {
  const current = sections.reduce(
    (active, section) => {
      const distance = Math.abs(section.getBoundingClientRect().top - 130);
      return distance < active.distance ? { id: section.id, distance } : active;
    },
    { id: "home", distance: Infinity },
  );
  nav.querySelectorAll("a").forEach((link) => {
    link.classList.toggle(
      "active",
      link.getAttribute("href") === `#${current.id}`,
    );
  });
};

activateNav();
window.addEventListener("scroll", activateNav, { passive: true });

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(
    () => toast.classList.remove("show"),
    2200,
  );
}

document.querySelectorAll(".copy-command").forEach((button) => {
  button.addEventListener("click", async () => {
    const command = button.dataset.command;
    try {
      await navigator.clipboard.writeText(command);
      button.querySelector(".copy-label").textContent = "Comando copiado";
      showToast("Comando copiado para a área de transferência");
      window.setTimeout(() => {
        button.querySelector(".copy-label").textContent = "Copiar comando";
      }, 2200);
    } catch {
      showToast("Não foi possível copiar automaticamente");
    }
  });
});

const releasesScript = document.createElement("script");
releasesScript.src = "js/versions.js";
document.head.appendChild(releasesScript);
