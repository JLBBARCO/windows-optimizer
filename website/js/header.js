const header = document.querySelector("header");

const mainSections = document.querySelectorAll("main>section");
let headerButtonsList = [];
mainSections.forEach((section) => {
  let array = { id: section.id, name: section.ariaLabel };
  headerButtonsList.push(array);
});

const nav = document.createElement("nav");
nav.classList.add("site-controls");
const ul = document.createElement("ul");
headerButtonsList.forEach((button) => {
  const li = document.createElement("li");
  const a = document.createElement("a");
  a.href = `#${button.id}`;
  a.textContent = button.name;
  li.appendChild(a);
  ul.appendChild(li);
});
nav.appendChild(ul);
header.appendChild(nav);
