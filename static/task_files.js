(()=>{
async function loadTaskFiles(){
 const list=document.getElementById('taskList');
 if(!list)return;
 const token=localStorage.mediaToken||'';
 const headers=token?{Authorization:'Bearer '+token}:{};
 const tasks=await fetch('/api/tasks',{headers}).then(r=>r.json());
 const cards=[...list.querySelectorAll('.task')];
 for(let i=0;i<cards.length&&i<tasks.length;i++){
  const t=tasks[i];
  let box=cards[i].querySelector('.task-file');
  if(!box){box=document.createElement('div');box.className='task-file';cards[i].appendChild(box);}
  if(t.status!=='completed'&&t.status!=='expired')continue;
  const d=await fetch('/api/tasks/'+t.id+'/file-status',{headers}).then(r=>r.json());
  if(d.exists){box.textContent='文件：'+d.name+' ('+d.size+' bytes)';}
  else{box.textContent='文件已自动清除 原解析链接：'+(d.source_url||'');}
 }
}
window.loadTaskFiles=loadTaskFiles;
setInterval(loadTaskFiles,5000);
})();