document.addEventListener("DOMContentLoaded", () => {
  const metadata = document.querySelector("meta[name='operon-language-url']");
  const label = document.querySelector("meta[name='operon-language-label']");
  const menu = document.querySelector(".wy-menu.wy-menu-vertical");

  if (!metadata || !label || !metadata.content || !menu) {
    return;
  }

  const container = document.createElement("div");
  container.className = "operon-language-switcher";

  const link = document.createElement("a");
  link.href = metadata.content;
  link.hreflang = label.content === "中文" ? "zh" : "en";
  link.textContent = label.content;

  container.appendChild(link);
  menu.parentNode.insertBefore(container, menu);
});
