const API_BASE = "https://licht.onrender.com";


const section = document.getElementById("ReportSection");
const topicInput = document.getElementById("topic");
let currentSource = null;

const btnDownload = document.getElementById("btnDownload");
let currentTopic = "";

document.getElementById("btnReport").addEventListener("click", generateReport);

// optional: submit with the Enter key
topicInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") generateReport();
});

function generateReport() {
    const topic = topicInput.value.trim();
    if (!topic) return;

    const status = document.getElementById("status");
    const statusText = document.getElementById("status-text");
    const reportEl = document.getElementById("para_Report");

    // close any request that is still running (e.g. a second submit)
    if (currentSource) currentSource.close();

    section.classList.add("has-report");   // triggers the transition

    currentTopic = topic;                  // used for the PDF file name
    btnDownload.style.display = "none";    // hide until the new report is ready

    reportEl.innerHTML = "";
    statusText.textContent = "Starting...";
    status.style.display = "flex";

    const es = new EventSource(
        `${API_BASE}/getReport?topic=${encodeURIComponent(topic)}`
    );
    currentSource = es;

    es.onmessage = (e) => {
        const data = JSON.parse(e.data);

        if (data.type === "status") {
            statusText.textContent = data.message;
        } else if (data.type === "report") {
            reportEl.innerHTML = DOMPurify.sanitize(marked.parse(data.report));
            status.style.display = "none";
            btnDownload.style.display = "block";      // show the download button
            es.close();
        } else if (data.type === "error") {
            statusText.textContent = "Something went wrong: " + data.message;
            btnDownload.style.display = "none";
            section.classList.remove("has-report");   // back to the start screen
            es.close();
        }
    };

    es.onerror = () => {
        statusText.textContent = "Connection lost.";
        btnDownload.style.display = "none";
        section.classList.remove("has-report");       // back to the start screen
        es.close();
    };
}

btnDownload.addEventListener("click", () => {
    const reportEl = document.getElementById("para_Report");

    const safeName = currentTopic.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "");

    const options = {
        margin:       [15, 15, 15, 15],               // top, left, bottom, right in mm
        filename:     `Licht-${safeName || "report"}.pdf`,
        image:        { type: "jpeg", quality: 0.98 },
        html2canvas:  { scale: 2, useCORS: true },
        jsPDF:        { unit: "mm", format: "a4", orientation: "portrait" },
        pagebreak:    { mode: ["css", "legacy"], avoid: ["h1", "h2", "h3", "li", "tr"] }
    };

    btnDownload.textContent = "Preparing PDF...";
    btnDownload.disabled = true;
    reportEl.classList.add("pdf-mode");               // fixed-width layout for the export

    html2pdf().set(options).from(reportEl).save().finally(() => {
        reportEl.classList.remove("pdf-mode");
        btnDownload.textContent = "Download PDF";
        btnDownload.disabled = false;
    });
});