// Appearance: System → Light → Dark, matching the iOS app.
const themeModes = ["system", "light", "dark"];

function applyTheme(mode) {
  const validMode = themeModes.includes(mode) ? mode : "system";
  const root = document.documentElement;

  if (validMode === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", validMode);
  }

  const iconClasses = {
    system: "fa-solid fa-circle-half-stroke",
    light: "fa-solid fa-sun",
    dark: "fa-solid fa-moon",
  };

  document.querySelectorAll(".theme-toggle").forEach((button) => {
    const icon = button.querySelector("i");
    const label = button.querySelector(".theme-label");
    const title = validMode.charAt(0).toUpperCase() + validMode.slice(1);

    button.dataset.mode = validMode;
    button.setAttribute("aria-label", `Appearance: ${title}. Activate for the next option.`);
    button.title = `Appearance: ${title}`;
    if (icon) icon.className = iconClasses[validMode];
    if (label) label.textContent = title;
  });
}

function toggleTheme() {
  const currentMode = localStorage.getItem("theme") || "system";
  const currentIndex = themeModes.indexOf(currentMode);
  const nextMode = themeModes[(Math.max(currentIndex, 0) + 1) % themeModes.length];
  localStorage.setItem("theme", nextMode);
  applyTheme(nextMode);
}

function initializeTheme() {
  applyTheme(localStorage.getItem("theme") || "system");
}

// Only known subjects and chapters reach URLs and the page: inputs from the query
// string or localStorage are looked up in chapterData, and the stored strings are
// used from then on, never the raw values.
function knownSubject(value) {
  return Object.keys(chapterData).find((name) => name === value);
}

function knownChapter(subject, value) {
  return subject ? chapterData[subject].find((name) => name === value) : undefined;
}

// Navigation Functions
function navigateToSubject(subject) {
  const known = knownSubject(subject);
  window.location.href = known ? `/${known}` : "/";
}

function goBack() {
  const subject = knownSubject(localStorage.getItem("currentSubject"));
  window.location.href = subject ? `/${subject}` : "/";
}


// One catalog is shared with the native app and verified against the source pages.
let chapterCatalog = {};
let chapterData = {};

async function loadCatalog() {
  const response = await fetch("/catalog.json");
  if (!response.ok) throw new Error("The chapter catalog could not be loaded.");
  chapterCatalog = await response.json();
  chapterData = Object.fromEntries(
    Object.entries(chapterCatalog).map(([subject, chapters]) => [
      subject, chapters.map((chapter) => chapter.folderName),
    ]),
  );
}

// Load chapters for subject pages
function loadChapters(subject) {
  const chapterList = document.getElementById("chapter-list");
  const searchInput = document.getElementById("chapter-search");

  if (!chapterList || !chapterData[subject]) return;

  const renderChapters = (chapters) => {
    chapterList.innerHTML = "";

    if (chapters.length === 0) {
      chapterList.innerHTML = '<div class="empty-state">No chapter matches that search.</div>';
    }

    chapters.forEach((chapter) => {
      const chapterCard = document.createElement("button");
      const chapterNumber = chapter.split(".")[0];
      const chapterName = chapter.split(".").slice(1).join(".").trim();
      const pageCount = getImageCount(subject, chapter);

      chapterCard.type = "button";
      chapterCard.className = "chapter-card";
      chapterCard.setAttribute(
        "aria-label",
        `Chapter ${chapterNumber}, ${chapterName}, ${pageCount} pages`,
      );
      chapterCard.onclick = () => openChapter(subject, chapter);
      chapterCard.innerHTML = `
        <span class="margin-number" aria-hidden="true">${String(chapterNumber).padStart(2, "0")}</span>
        <span class="row-copy">
          <span class="chapter-title">Chapter ${chapterNumber}</span>
          <span class="chapter-subtitle">${chapterName}</span>
        </span>
        <span class="chapter-pages">${pageCount} pages</span>
        <span class="row-arrow" aria-hidden="true">↗</span>
      `;
      chapterList.appendChild(chapterCard);
    });

    const count = document.getElementById("chapter-count");
    if (count) count.textContent = `${chapters.length} ${chapters.length === 1 ? "chapter" : "chapters"}`;
  };

  renderChapters(chapterData[subject]);

  if (searchInput) {
    searchInput.addEventListener("input", () => {
      const query = searchInput.value.trim().toLocaleLowerCase();
      const filtered = chapterData[subject].filter((chapter) =>
        chapter.toLocaleLowerCase().includes(query),
      );
      renderChapters(filtered);
    });
  }
}

// Open chapter and load images
function openChapter(subject, chapterName) {
  // Store chapter info in localStorage for the gallery page
  localStorage.setItem("currentSubject", subject);
  localStorage.setItem("currentChapter", chapterName);

  const params = new URLSearchParams({ subject, chapter: chapterName });
  window.location.href = `/gallery?${params.toString()}`;
}

// Gallery Functions
function loadGallery() {
  const params = new URLSearchParams(window.location.search);
  const subject = knownSubject(params.get("subject") || localStorage.getItem("currentSubject"));
  const chapter = knownChapter(subject, params.get("chapter") || localStorage.getItem("currentChapter"));

  if (!subject || !chapter) {
    window.location.href = "/";
    return;
  }

  localStorage.setItem("currentSubject", subject);
  localStorage.setItem("currentChapter", chapter);
  document.body.dataset.subject = subject;

  const galleryTitle = document.getElementById("gallery-title");
  const imageGallery = document.getElementById("image-gallery");

  if (!galleryTitle || !imageGallery) return;

  const chapterNumber = chapter.split(".")[0];
  const chapterName = chapter.split(".").slice(1).join(".").trim();

  const subjectName = subject.charAt(0).toUpperCase() + subject.slice(1);
  const pageCount = getImageCount(subject, chapter);
  galleryTitle.textContent = `${subjectName} · ${chapterName}`;
  document.title = `${chapterName} — PCM Notes`;

  const backLink = document.getElementById("reader-back");
  if (backLink) backLink.href = `/${subject}`;

  const countLabel = document.getElementById("reader-page-count");
  if (countLabel) countLabel.textContent = `Chapter ${chapterNumber} · ${pageCount} handwritten pages`;

  loadImages(subject, chapter);
  initializeReaderProgress();
}

function loadImages(subject, chapter) {
  const imageGallery = document.getElementById("image-gallery");
  if (!imageGallery) return;

  imageGallery.innerHTML = "";
  const imageCount = getImageCount(subject, chapter);

  if (imageCount === 0) {
    imageGallery.innerHTML = '<div class="empty-state">No handwritten pages were found for this chapter.</div>';
    return;
  }

  for (let i = 1; i <= imageCount; i++) {
    const imageNumber = String(i).padStart(4, "0");
    const imagePath = `${subject}/${chapter}/${imageNumber}.jpg`;

    const figure = document.createElement("figure");
    const img = document.createElement("img");
    const caption = document.createElement("figcaption");

    figure.className = "note-page";
    img.src = imagePath;
    img.alt = `${subject} handwritten chapter notes, page ${i} of ${imageCount}`;
    img.loading = i === 1 ? "eager" : "lazy";
    img.decoding = "async";
    img.className = "gallery-image";
    if (i === 1) img.fetchPriority = "high";
    caption.className = "page-caption";
    caption.textContent = `${i} / ${imageCount}`;

    img.onerror = function () {
      figure.remove();
    };

    figure.append(img, caption);
    imageGallery.appendChild(figure);
  }
}

function initializeReaderProgress() {
  const update = () => {
    const scrollable = document.documentElement.scrollHeight - window.innerHeight;
    const progress = scrollable > 0 ? Math.min((window.scrollY / scrollable) * 100, 100) : 0;
    document.documentElement.style.setProperty("--reader-progress", `${progress}%`);
  };

  update();
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
}

function getImageCount(subject, chapter) {
  return chapterCatalog[subject]?.find((entry) => entry.folderName === chapter)?.pageCount || 0;
}

// Navigation helper
function navigateToSubjectPage(subject) {
  navigateToSubject(subject);
}

// PDF Download Function
async function downloadChapterPDF() {
  const subject = knownSubject(localStorage.getItem("currentSubject"));
  const chapter = knownChapter(subject, localStorage.getItem("currentChapter"));

  if (!subject || !chapter) {
    alert("Error: Chapter information not found");
    return;
  }

  const imageCount = getImageCount(subject, chapter);
  if (imageCount === 0) {
    alert("No images found in this chapter");
    return;
  }

  const downloadBtn = document.getElementById("download-pdf");
  const originalText = downloadBtn.innerHTML;
  downloadBtn.disabled = true;
  downloadBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Generating...';

  try {
    const { jsPDF } = window.jspdf;

    // Create PDF in portrait mode for handwritten notes
    const pdf = new jsPDF({
      orientation: "portrait",
      unit: "mm",
      format: "a4",
    });

    const pageWidth = pdf.internal.pageSize.getWidth();
    const pageHeight = pdf.internal.pageSize.getHeight();
    const margin = 5;
    const maxWidth = pageWidth - margin * 2;
    const maxHeight = pageHeight - margin * 2;

    let addedPages = 0;

    for (let i = 1; i <= imageCount; i++) {
      const imageNumber = String(i).padStart(4, "0");
      const imagePath = `${subject}/${chapter}/${imageNumber}.jpg`;

      // Update button with progress
      downloadBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${i}/${imageCount}`;

      try {
        // Load image and convert to base64
        const result = await loadImageForPDF(imagePath);

        if (result && result.dataUrl) {
          // Calculate dimensions to fit on page while maintaining aspect ratio
          const widthRatio = maxWidth / result.width;
          const heightRatio = maxHeight / result.height;
          const ratio = Math.min(widthRatio, heightRatio);

          const imgWidth = result.width * ratio;
          const imgHeight = result.height * ratio;

          // Center image on page
          const x = (pageWidth - imgWidth) / 2;
          const y = (pageHeight - imgHeight) / 2;

          if (addedPages > 0) {
            pdf.addPage();
          }

          pdf.addImage(result.dataUrl, "JPEG", x, y, imgWidth, imgHeight);
          addedPages++;
        }
      } catch (imgError) {
        console.error(`Error loading image ${i}:`, imgError);
      }
    }

    if (addedPages === 0) {
      alert("Could not load any images. Please try again.");
      return;
    }

    // Generate filename from chapter name
    const chapterName = chapter.replace(/[^a-zA-Z0-9\s.-]/g, "").trim();
    pdf.save(`${chapterName}.pdf`);
  } catch (error) {
    console.error("PDF generation error:", error);
    alert("Error generating PDF: " + error.message);
  } finally {
    downloadBtn.disabled = false;
    downloadBtn.innerHTML = originalText;
  }
}

// Helper function to load image for PDF
function loadImageForPDF(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();

    img.onload = function () {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#FFFFFF";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0);
        const dataUrl = canvas.toDataURL("image/jpeg", 0.85);
        resolve({
          dataUrl: dataUrl,
          width: img.naturalWidth,
          height: img.naturalHeight
        });
      } catch (e) {
        reject(e);
      }
    };

    img.onerror = function() {
      reject(new Error("Failed to load image: " + src));
    };

    // Add cache-busting and load
    img.src = src + "?t=" + Date.now();
  });
}

// Initialize page based on current location
async function initializePage() {
  initializeTheme();

  document.querySelectorAll("[data-current-year]").forEach((element) => {
    element.textContent = String(new Date().getFullYear());
  });

  const path = window.location.pathname.replace(/\/+$/, "");
  const page = path.substring(path.lastIndexOf("/") + 1).replace(/\.html$/i, "");

  if (["physics", "chemistry", "mathematics", "gallery"].includes(page)) {
    try {
      await loadCatalog();
    } catch (error) {
      const container = document.getElementById("chapter-list") || document.getElementById("image-gallery");
      if (container) container.textContent = "Notes could not be loaded. Please refresh to try again.";
      const download = document.getElementById("download-pdf");
      if (download) download.disabled = true;
      console.error(error);
      return;
    }
  }

  if (page === "physics") {
    loadChapters("physics");
  } else if (page === "chemistry") {
    loadChapters("chemistry");
  } else if (page === "mathematics") {
    loadChapters("mathematics");
  } else if (page === "gallery") {
    loadGallery();
  }
}

// Wait for DOM to be loaded
document.addEventListener("DOMContentLoaded", function () {
  initializePage();
});
