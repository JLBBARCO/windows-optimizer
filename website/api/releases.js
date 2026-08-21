const repository = "JLBBARCO/windows-optimizer";
const githubApiUrl = `https://api.github.com/repos/${repository}/releases`;

function getDownloadAsset(assets) {
  return (
    assets.find((asset) =>
      /portable|port[aá]vel|\.zip$|\.exe$/i.test(asset.name),
    ) || assets[0]
  );
}

function normalizeRelease(release) {
  const downloadAsset = getDownloadAsset(release.assets || []);

  return {
    release: release.name || release.tag_name,
    version: release.tag_name,
    release_date: release.published_at || release.created_at,
    download_link: downloadAsset?.browser_download_url || release.html_url,
    prerelease: release.prerelease,
  };
}

async function getAllReleases() {
  const releases = [];

  for (let page = 1; ; page += 1) {
    const response = await fetch(`${githubApiUrl}?per_page=100&page=${page}`, {
      headers: {
        Accept: "application/vnd.github+json",
        "User-Agent": "windows-optimizer-website",
      },
    });

    if (!response.ok) {
      throw new Error(
        `GitHub releases request failed with status ${response.status}`,
      );
    }

    const pageReleases = await response.json();
    releases.push(
      ...pageReleases.filter((release) => !release.draft).map(normalizeRelease),
    );

    if (pageReleases.length < 100) {
      return releases;
    }
  }
}

module.exports = async function handler(request, response) {
  if (request.method !== "GET") {
    response.setHeader("Allow", "GET");
    return response.status(405).json({ error: "Method not allowed" });
  }

  try {
    const releases = await getAllReleases();
    response.setHeader(
      "Cache-Control",
      "s-maxage=3600, stale-while-revalidate=300",
    );
    return response.status(200).json(releases);
  } catch (error) {
    console.error(error);
    return response
      .status(502)
      .json({ error: "Unable to fetch repository releases" });
  }
};
