const btn = document.getElementById("btnAllow");
const result = document.getElementById("result");
const hint = document.getElementById("hint");

btn.addEventListener("click", async () => {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach(t => t.stop());

    result.style.display = "block";
    result.className = "result result-ok";
    result.textContent = "Microphone access granted! You can close this tab.";
    btn.style.display = "none";
    hint.style.display = "none";

    chrome.storage.local.set({ micDenied: false });
  } catch (err) {
    result.style.display = "block";
    result.className = "result result-fail";

    if (err.name === "NotAllowedError") {
      result.textContent = "Access denied. Please allow it manually:";
      hint.style.display = "block";
      hint.textContent =
        "1. Click the lock/tune icon in the address bar above\n" +
        "2. Find Microphone and set it to Allow\n" +
        "3. Reload this page and click the button again";
      hint.style.whiteSpace = "pre-line";
    } else {
      result.textContent = "Error: " + err.message;
    }
  }
});
