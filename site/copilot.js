"use strict";

const params = new URLSearchParams(window.location.hash.slice(1));
const prompts = params.getAll("prompt");

if (prompts.length !== 1 || !prompts[0].trim()) {
  document.getElementById("status").textContent =
    "Missing summary prompt. Please open a Summarise with Copilot link from a digest.";
} else {
  const prompt = prompts[0];
  // Only the prompt is supplied by the link; the destination is always Copilot.
  const destination = new URL("https://github.com/copilot");
  destination.searchParams.set("prompt", prompt);
  document.getElementById("prompt").value = prompt;
  const continueLink = document.getElementById("continue");
  continueLink.href = destination.href;
  continueLink.hidden = false;
  document.getElementById("status").textContent = "Opening Copilot in your browser…";
  window.location.replace(destination.href);
}
