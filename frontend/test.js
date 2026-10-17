const section = document.getElementById("ReportSection");
const topicInput = document.getElementById("topic");
let currentSource = null;

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

    reportEl.innerHTML = "";
    statusText.textContent = "Starting...";
    status.style.display = "flex";

    const es = new EventSource(
        `http://127.0.0.1:8000/getReport?topic=${encodeURIComponent(topic)}`
    );
    currentSource = es;

    es.onmessage = (e) => {
        const data = JSON.parse(e.data);

        if (data.type === "status") {
            statusText.textContent = data.message;
        } else if (data.type === "report") {
            reportEl.innerHTML = DOMPurify.sanitize(marked.parse(data.report));
            status.style.display = "none";
            es.close();
        } else if (data.type === "error") {
            statusText.textContent = "Something went wrong: " + data.message;
            section.classList.remove("has-report");   // back to the start screen
            es.close();
        }
    };

    es.onerror = () => {
        statusText.textContent = "Connection lost.";
        section.classList.remove("has-report");       // back to the start screen
        es.close();
    };
}