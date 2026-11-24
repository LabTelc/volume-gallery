// The "back" button of the viewers: to the folder the item is in (the gallery shows a folder from
// /index.html?folder=<id>), or to the top of the gallery for an item that is not in a folder.
// A folder the user cannot see (any more) is shown by the gallery as the top level, so the link is safe either way.

const ARROW_LEFT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5m0 0 6-6m-6 6 6 6"/></svg>`;

/** Where the gallery shows `folderId` (null / undefined: the top level). */
export function folderHref(folderId) {
  return folderId == null ? "/index.html" : `/index.html?folder=${encodeURIComponent(folderId)}`;
}

/** The button for the top bar of a viewer; `asset` is what the API returned for the item (it has a folder_id). */
export function backToFolderButton(asset) {
  const inFolder = asset?.folder_id != null;
  const label = inFolder ? "Back to folder" : "Back to gallery";
  const title = inFolder ? "Back to the folder this item is in" : "Back to the gallery";
  // (a link around the button, as the other buttons of the viewers that navigate: it opens in a new tab too)
  return `<a class="viewer-back" href="${folderHref(asset?.folder_id)}" title="${title}"><button type="button" class="icon-btn">${ARROW_LEFT}<span class="icon-btn-label">${label}</span></button></a>`;
}
