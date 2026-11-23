const docs = document.querySelector("#documents");
async function request(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}
async function refresh() {
  const data = await request("/resume/documents");
  docs.replaceChildren(...data.items.map(item => { const li=document.createElement("li"); li.textContent=`${item.filename} · ${new Date(item.created_at).toLocaleString()}`; return li; }));
}
document.querySelector("#upload").addEventListener("submit", async event => {
  event.preventDefault(); const status=document.querySelector("#upload-status"); status.textContent="正在读取并建立检索索引…";
  try { const body=new FormData(); body.append("file", event.currentTarget.elements.file.files[0]); const result=await request("/resume/documents",{method:"POST",body}); status.textContent=`已加入 ${result.chunks_added} 个检索片段。`; await refresh(); }
  catch(error) { status.textContent=error.message; }
});
document.querySelector("#match").addEventListener("submit", async event => {
  event.preventDefault(); const status=document.querySelector("#match-status"); const output=document.querySelector("#analysis"); status.textContent="正在检索个人材料并分析…"; output.replaceChildren();
  try { const result=await request("/resume/match",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({job_description:event.currentTarget.elements.job_description.value})}); status.textContent=`已参考 ${result.sources.length} 段个人材料。`; const text=document.createElement("p"); text.style.whiteSpace="pre-wrap"; text.textContent=result.analysis; output.append(text); }
  catch(error) { status.textContent=error.message; }
});
refresh().catch(error=>{docs.textContent=error.message;});
