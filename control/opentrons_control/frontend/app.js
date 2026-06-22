const fileInput = document.getElementById("fileInput");
const uploadBtn = document.getElementById("uploadBtn");
const output = document.getElementById("output");

uploadBtn.onclick = async () => {
  const file = fileInput.files[0];
  if (!file) {
    alert("Select a file first");
    return;
  }

  const form = new FormData();
  form.append("file", file);

  const response = await fetch("/upload", {
    method: "POST",
    body: form,
  });

  const data = await response.json();
  output.textContent = JSON.stringify(data, null, 2);
};
