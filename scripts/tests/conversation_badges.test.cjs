const {test}=require('node:test');const assert=require('node:assert/strict');
require('../../static/js/conversation_badges.js');
const tick=()=>new Promise(r=>setTimeout(r,5));
test('completion badge clears only after successful acknowledgement',async()=>{
 let row={thread_id:'x',answer_id:5,unread:true,running:false},rendered,posted;
 const badge=FosConversationBadges.create({csrf:()=> 'token',interval:10000,onState:r=>rendered=r,fetchImpl:async(url,options)=>{
  if(options.method==='POST'){posted=options;return {ok:true};}
  return {ok:true,json:async()=>({conversations:[row]})};
 }});
 badge.start();await tick();assert.equal(rendered.unread,true);
 await badge.read('x');assert.equal(rendered.unread,false);assert.equal(posted.body,'answer_id=5');badge.pause();
});
test('failed acknowledgement retains unread badge',async()=>{
 let rendered;const badge=FosConversationBadges.create({csrf:()=>'',interval:10000,onState:r=>rendered=r,fetchImpl:async(url,options)=> options.method==='POST'?{ok:false}:{ok:true,json:async()=>({conversations:[{thread_id:'x',answer_id:5,unread:true}]})}});
 badge.start();await tick();assert.equal(await badge.read('x'),false);assert.equal(rendered.unread,true);badge.pause();
});
test('late status response does not update a page after leaving',async()=>{
 let resolve;const badge=FosConversationBadges.create({csrf:()=>'',onState:()=>assert.fail(),fetchImpl:()=>new Promise(r=>resolve=r)});
 badge.start();badge.pause();resolve({ok:true,json:async()=>({conversations:[{thread_id:'x'}]})});await tick();
});
