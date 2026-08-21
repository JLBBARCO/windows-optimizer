const table_versions = document.getElementById("table_versions");
const tableBody = table_versions.querySelector("tbody");

function addReleaseRow(version) {
  const row = document.createElement("tr");
  const release = document.createElement("td");
  const versionCell = document.createElement("td");
  const releaseDate = document.createElement("td");
  const downloadCell = document.createElement("td");
  const download = document.createElement("a");
  const downloadIcon = document.createElement("span");

  release.className = "releases";
  release.textContent = version.release;
  versionCell.className = "version";
  versionCell.textContent = version.version;
  releaseDate.className = "release-date";
  releaseDate.textContent = new Date(version.release_date).toLocaleDateString(
    "pt-BR",
  );
  downloadCell.className = "download-link";
  download.className = "download_link";
  download.href = version.download_link;
  download.target = "_blank";
  download.rel = "noopener noreferrer";
  downloadIcon.className = "material-symbols-outlined";
  downloadIcon.textContent = "download";

  download.appendChild(downloadIcon);
  downloadCell.appendChild(download);
  row.append(release, versionCell, releaseDate, downloadCell);
  tableBody.appendChild(row);
}

async function loadVersions() {
  tableBody.textContent = "Carregando versões...";

  try {
    const response = await fetch("/api/releases");
    if (!response.ok) {
      throw new Error(`Release request failed with status ${response.status}`);
    }

    const versions = await response.json();
    versions.sort(
      (first, second) =>
        new Date(second.release_date) - new Date(first.release_date),
    );
    tableBody.textContent = "";
    versions.forEach(addReleaseRow);
  } catch (error) {
    console.error(error);
    tableBody.textContent = "Não foi possível carregar as versões.";
  }
}

loadVersions();
