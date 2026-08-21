const jsonContact =
  "https://raw.githubusercontent.com/JLBBARCO/portfolio/refs/heads/main/src/json/areas/contact.json";

const footer = document.querySelector("footer");
const iconNames = {
  email: "fa-solid fa-envelope",
  github: "fa-brands fa-github",
  linkedin: "fa-brands fa-linkedin",
};

function createContactCard(card) {
  const link = document.createElement("a");
  const icon = document.createElement("i");
  const name = document.createElement("span");

  link.className = "contact-card";
  link.href = card.url;
  link.target = card.url.startsWith("http") ? "_blank" : "_self";
  link.rel = "noopener noreferrer";
  link.setAttribute("aria-label", card.name);

  link.addEventListener("click", (e) => {
    e.preventDefault();
    copyToClipboard(card.url);
  });

  icon.className = iconNames[card.iconName] || "fa-solid fa-link";
  icon.setAttribute("aria-hidden", "true");
  name.textContent = card.name;

  link.append(icon, name);
  return link;
}

async function loadContactCards() {
  try {
    const response = await fetch(jsonContact);
    if (!response.ok) {
      throw new Error(`Contact request failed with status ${response.status}`);
    }

    const contact = await response.json();
    const cards = document.createElement("div");
    cards.className = "contact-cards";
    contact.cards.forEach((card) => cards.appendChild(createContactCard(card)));
    footer.replaceChildren(cards);
  } catch (error) {
    console.error(error);
  }
}

loadContactCards();
