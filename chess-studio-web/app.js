import { Chess } from './chess-engine.js';

const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const homeView = $('#homeView');
const contentView = $('#contentView');
const stage = $('#stage');
const toastEl = $('#toast');
const PIECES = {
  w: {k:'♔',q:'♕',r:'♖',b:'♗',n:'♘',p:'♙'},
  b: {k:'♚',q:'♛',r:'♜',b:'♝',n:'♞',p:'♟'}
};
const VALUE = {p:100,n:320,b:335,r:500,q:900,k:20000};
const state = {
  screen:'home', mode:'bot', game:null, selected:null, legal:[], lastMove:null, flipped:false,
  difficulty:'beginner', puzzle:null, session:readSession(), matchPoll:null, gamePoll:null,
  onlineGameId:null, lastOnlineGame:null, onlineBoard:new Chess(), onlineChat:[], previousMoveCount:0,
  selectedTime:'blitz', botThinking:false, sessionMe:null
};

function readSession(){ try { return JSON.parse(localStorage.getItem('chess-studio-session') || 'null'); } catch { return null; } }
function saveSession(value){ state.session=value; if(value) localStorage.setItem('chess-studio-session',JSON.stringify(value)); else localStorage.removeItem('chess-studio-session'); updateAccountChip(); }
function updateAccountChip(){ $('#accountName').textContent = state.session?.username?.toUpperCase() || 'GUEST PLAYER'; $('#accountBtn .avatar').textContent = state.session ? '♟' : '♞'; }
function toast(msg){ toastEl.textContent=msg; toastEl.classList.add('show'); clearTimeout(toast._timer); toast._timer=setTimeout(()=>toastEl.classList.remove('show'),2800); }
function escapeHtml(v){return String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function stopPolling(){ if(state.matchPoll) clearInterval(state.matchPoll); if(state.gamePoll) clearInterval(state.gamePoll); state.matchPoll=state.gamePoll=null; }
function showHome(){ stopPolling(); state.screen='home'; homeView.classList.remove('hidden'); contentView.classList.add('hidden'); contentView.innerHTML=''; updateAccountChip(); }
function screen(name){
  stopPolling(); state.screen=name; homeView.classList.add('hidden'); contentView.classList.remove('hidden');
  const renderers={online:renderOnline,basics:renderBasics,bot:()=>renderBotSetup('bot'),coach:()=>renderBotSetup('coach'),local:()=>startLocalGame(),boss:()=>renderBotSetup('boss'),puzzles:renderPuzzles,inventory:renderInventory};
  (renderers[name] || showHome)();
}
function head(title, kicker=''){return `<div class="screen-head"><div><div class="subtitle" style="text-align:left;letter-spacing:1.8px">${kicker || 'CHESS STUDIO'}</div><h2>${title}</h2></div><button class="back-btn" data-action="home">← Menu</button></div>`;}
function resetChess(){state.game=new Chess();state.selected=null;state.legal=[];state.lastMove=null;state.flipped=false;state.botThinking=false;}

async function apiPost(path,data={}){
  const payload={...data}; if(state.session?.token) payload.token=state.session.token;
  try {
    const response=await fetch(`/api/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    return await response.json();
  } catch(e){return {ok:false,error:'Could not reach the Chess Studio server. Check your connection and try again.'};}
}

function renderBotSetup(mode){
  state.mode=mode; const names={bot:'Play vs Bot',coach:'Play vs Coach',boss:'FINAL BOSS'};
  const blurb={bot:'Choose a rival and make your first move.',coach:'Your coach plays back and leaves room to learn.',boss:'A tougher challenge for players who are ready.'};
  contentView.innerHTML=head(names[mode], 'CHOOSE YOUR MATCH')+`<div class="paper-card"><h3>${names[mode]}</h3><p>${blurb[mode]}</p><label class="field-label">YOUR COLOUR</label><div class="choice-row"><button class="choice selected" data-color="white">♔ White</button><button class="choice" data-color="black">♚ Black</button></div><label class="field-label">OPPONENT STRENGTH</label><div class="choice-row" id="strengthChoices"><button class="choice ${mode==='bot'?'selected':''}" data-level="beginner">Beginner</button><button class="choice ${mode==='coach'?'selected':''}" data-level="intermediate">Intermediate</button><button class="choice ${mode==='boss'?'selected':''}" data-level="boss">Grandmaster</button></div><p class="note">${mode==='boss'?'The Final Boss searches deeper before choosing moves.':'Practice locally in this browser — no account needed.'}</p><div class="section-actions"><button class="gold-btn" data-action="start-bot">Start game ♟</button><button class="small-btn" data-action="home">Not yet</button></div></div>`;
  state.playerColor='white'; state.difficulty= mode==='boss'?'boss':mode==='coach'?'intermediate':'beginner';
}
function startLocalGame(){ resetChess(); state.mode='local'; state.playerColor='white'; state.difficulty='beginner'; renderGame('local'); }
function startBotGame(){ resetChess(); state.mode=state.mode==='coach'?'coach':state.mode==='boss'?'boss':'bot'; state.playerColor=state.playerColor||'white'; if(state.playerColor==='black') state.flipped=true; renderGame(state.mode); if(state.game.turn()===(state.playerColor==='white'?'b':'w')) setTimeout(botMove,450); }

function renderGame(mode, onlineView=null){
  state.screen=mode==='online-game'?'online-game':'game';
  let name=mode==='local'?'2 Player Match':mode==='coach'?'Coach Match':mode==='boss'?'FINAL BOSS':mode==='online-game'?'Play Online':'Play vs Bot';
  const online=mode==='online-game';
  state.selected=null;state.legal=[];
  if(online)state.flipped=onlineView?.you==='black';
  contentView.innerHTML=head(name,online?'RATED ONLINE MATCH':'READY WHEN YOU ARE')+`<div class="board-layout">
    <div class="board-wrap"><div class="player-strip" style="margin-bottom:7px"><span class="player-icon">${online?'♟':'♚'}</span><span>${online?escapeHtml(onlineView?.players?.[state.session?.username?.toLowerCase()===onlineView?.players?.white?.name?.toLowerCase()?'black':'white']?.name || 'Opponent'):(mode==='local'?'Player 2':mode==='boss'?'The Final Boss':mode==='coach'?'Chess Coach':'Chess Bot')}</span><span class="clock" id="topClock">—</span></div><div class="chess-board" id="chessBoard" role="grid" aria-label="Chess board"></div><div class="player-strip" style="margin-top:7px"><span class="player-icon">♔</span><span>${online?escapeHtml(state.session?.username || 'You'):'You'}</span><span class="clock" id="bottomClock">—</span></div></div>
    <aside class="game-side"><div class="game-status" id="gameStatus">${online?'Connecting to opponent…':'White to move.'}</div><div class="paper-card" style="padding:10px;margin:0"><h3 style="font-size:16px">Moves</h3><div class="move-list" id="moveList">No moves yet.</div></div><div class="game-tools">${online?'<button class="small-btn" data-action="offer-draw">Offer draw</button><button class="small-btn" data-action="resign">Resign</button>':'<button class="small-btn" data-action="flip">Flip board</button><button class="small-btn" data-action="undo">Undo move</button>'}<button class="small-btn" data-action="new-game">New game</button><button class="small-btn" data-action="home">Exit</button></div>${online?'<div class="chat-box"><b style="font-size:13px">Match chat</b><div class="chat-log" id="chatLog"></div><form class="chat-form" id="chatForm"><input class="field" id="chatMessage" maxlength="200" placeholder="Say good game…"><button class="small-btn">Send</button></form></div>':'<div class="paper-card" style="padding:10px;margin:0"><b style="font-size:13px">Helpful hint</b><p class="note" id="hintText">Look for checks, captures and threats before you move.</p></div>'}</aside></div>`;
  if(online){state.lastOnlineGame=onlineView;updateOnlineGameUI(onlineView);} else {drawBoard(state.game);updateGameStatus();}
}
function getCurrentBoard(){return state.screen==='online-game'?state.onlineBoard:state.game;}
function drawBoard(game=getCurrentBoard()){
  const boardEl=$('#chessBoard'); if(!boardEl||!game)return;
  let squares=[]; const board=game.board();
  for(let row=0;row<8;row++) for(let col=0;col<8;col++){
    const file=state.flipped?7-col:col, rank=state.flipped?row:7-row;
    const square=String.fromCharCode(97+file)+(rank+1); const piece=board[7-rank][file];
    const light=(file+rank)%2===1; const selected=state.selected===square;
    let classes='square '+(light?'light':'dark');
    if(selected) classes+=' selected';
    if(state.lastMove?.includes(square)) classes+=' lastmove';
    const target=state.legal.find(m=>m.to===square); if(target) classes+=target.captured?' capture':' legal';
    let p=''; if(piece) p=`<span class="piece ${piece.color==='w'?'white-piece':'black-piece'}">${PIECES[piece.color][piece.type]}</span>`;
    squares.push(`<button class="${classes}" data-square="${square}" role="gridcell" aria-label="${square}${piece?' '+piece.type:''}">${p}</button>`);
  }
  boardEl.innerHTML=squares.join('');
}
function updateGameStatus(){
  const game=state.game; if(!game)return;
  let msg=game.isCheckmate()?'Checkmate!':game.isStalemate()?'Draw by stalemate.':game.isDraw()?'Draw game.':game.isCheck()?'Check! '+(game.turn()==='w'?'White':'Black')+' to move.':(game.turn()==='w'?'White':'Black')+' to move.';
  const el=$('#gameStatus');if(el)el.textContent=msg;
  const hist=game.history(); const moves=[];for(let i=0;i<hist.length;i+=2)moves.push(`${Math.floor(i/2)+1}. ${hist[i]}${hist[i+1]?' '+hist[i+1]:''}`);
  const ml=$('#moveList');if(ml)ml.textContent=moves.join('   ')||'No moves yet.';
  if(game.isGameOver()&&state.mode!=='local'){
    const hint=$('#hintText');if(hint)hint.textContent=game.isCheckmate()?'King captured by checkmate. Great game!':'Nice game. Want another round?';
  }
}
function onSquare(square){
  if(state.screen==='online-game')return onlineSquare(square);
  const game=state.game; if(!game||game.isGameOver()||state.botThinking)return;
  if(['bot','coach','boss'].includes(state.mode)&&game.turn()!==(state.playerColor==='white'?'w':'b'))return;
  const piece=game.get(square);
  if(state.selected){
    let move;
    try{move=game.move({from:state.selected,to:square,promotion:'q'});}catch{}
    if(move){state.lastMove=[move.from,move.to];state.selected=null;state.legal=[];drawBoard(game);updateGameStatus();
      if(state.mode==='puzzle'){
        const msg=$('#puzzleMessage');
        if(move.from+move.to+(move.promotion||'')===state.puzzle?.solution && game.isCheckmate()) { if(msg)msg.textContent='♛ Checkmate! Puzzle solved!'; toast('Puzzle solved — brilliant move!'); }
        else if(msg)msg.textContent='Not quite. Reset and look for the forcing move.';
      }
      if(['bot','coach','boss'].includes(state.mode)&&!game.isGameOver())setTimeout(botMove,220);
      return;
    }
  }
  if(piece&&piece.color===game.turn()){
    if(['bot','coach','boss'].includes(state.mode)&&piece.color!==(state.playerColor==='white'?'w':'b'))return;
    state.selected=square;state.legal=game.moves({square,verbose:true});drawBoard(game);
  }else{state.selected=null;state.legal=[];drawBoard(game);}
}
function evaluate(game,forColor){
  let score=0; for(let r=0;r<8;r++)for(let f=0;f<8;f++){const p=game.board()[r][f];if(!p)continue;let v=VALUE[p.type];if(p.type==='p')v+=(p.color==='w'?r:7-r)*5; if(['n','b'].includes(p.type))v+=(3.5-Math.abs(3.5-f)+3.5-Math.abs(3.5-r))*3;score+=(p.color===forColor?v:-v);} if(game.isCheckmate())score=game.turn()===forColor?-99999:99999;return score;
}
function botMove(){
  if(!state.game||state.game.isGameOver()||state.botThinking)return;
  const g=state.game; if(g.turn()===(state.playerColor==='white'?'w':'b'))return;
  state.botThinking=true;const status=$('#gameStatus');if(status)status.textContent='Thinking…';
  setTimeout(()=>{
    if(!state.game||state.game.isGameOver()){state.botThinking=false;return;}
    const moves=g.moves({verbose:true}); if(!moves.length){state.botThinking=false;updateGameStatus();return;}
    let chosen;
    if(state.difficulty==='boss') chosen=searchBestMove(g,Math.min(2, state.game.history().length<8?2:2));
    else if(state.difficulty==='intermediate') chosen=searchBestMove(g,1);
    else chosen=moves[Math.floor(Math.random()*moves.length)];
    try{const m=g.move({from:chosen.from,to:chosen.to,promotion:chosen.promotion||'q'});state.lastMove=[m.from,m.to];}catch{const m=g.move(moves[0]);state.lastMove=[m.from,m.to];}
    state.selected=null;state.legal=[];state.botThinking=false;drawBoard(g);updateGameStatus();
    const hint=$('#hintText');if(hint&&state.mode==='coach'&&!g.isGameOver())hint.textContent='Good habit: check whether your move leaves a piece undefended.';
  },state.difficulty==='boss'?500:state.mode==='coach'?450:250);
}
function searchBestMove(game,depth){
  const color=game.turn();const moves=game.moves({verbose:true});let best=-Infinity,bestMove=moves[0];
  for(const move of moves){let score=-Infinity;try{game.move({from:move.from,to:move.to,promotion:move.promotion||'q'});score= depth<=1 ? evaluate(game,color) : minimax(game,depth-1,-Infinity,Infinity,color);game.undo();}catch{try{game.undo()}catch{}}
    score+=Math.random()*1.5;if(score>best){best=score;bestMove=move;}}
  return bestMove;
}
function minimax(game,depth,alpha,beta,rootColor){
  if(depth===0||game.isGameOver())return evaluate(game,rootColor);
  const moves=game.moves({verbose:true});if(game.turn()===rootColor){let value=-Infinity;for(const m of moves){game.move({from:m.from,to:m.to,promotion:m.promotion||'q'});value=Math.max(value,minimax(game,depth-1,alpha,beta,rootColor));game.undo();alpha=Math.max(alpha,value);if(beta<=alpha)break;}return value;}
  let value=Infinity;for(const m of moves){game.move({from:m.from,to:m.to,promotion:m.promotion||'q'});value=Math.min(value,minimax(game,depth-1,alpha,beta,rootColor));game.undo();beta=Math.min(beta,value);if(beta<=alpha)break;}return value;
}

function renderBasics(){
 contentView.innerHTML=head('Chess Basics','LEARN THE PIECES')+`<div class="paper-card"><h3>Every move has a purpose.</h3><p>Choose a piece to learn how it moves. In chess, White moves first; protect your king, and look for checks, captures, and threats.</p><div class="progress"><i id="lessonProgress" style="width:14%"></i></div><p class="note" id="lessonDetail">Start with the pawn. It moves forward, but captures diagonally.</p></div><div class="teach-grid">${[
 ['♙','Pawn','Forward one square; two on its first move. Captures diagonally.'],['♖','Rook','Moves straight across ranks and files.'],['♘','Knight','Moves in an L-shape and can jump over pieces.'],['♗','Bishop','Moves diagonally for any number of clear squares.'],['♕','Queen','Combines the rook and bishop: straight or diagonal.'],['♔','King','Moves one square in any direction. Keep it safe!']].map(([p,n,d],i)=>`<button class="lesson-card" data-lesson="${i}"><span class="piece-large">${p}</span><b>${n}</b><p>${d}</p></button>`).join('')}</div><div class="paper-card" style="margin-top:13px"><h3>Two special moves</h3><p><b>Castling:</b> move your king two squares toward a rook, then bring the rook beside it, when the path and safety rules allow it.</p><p><b>Promotion:</b> a pawn reaching the far side becomes a queen, rook, bishop, or knight — usually a queen.</p><div class="section-actions"><button class="gold-btn" data-action="start-bot">Practice a game ♟</button></div></div>`;
}
function renderPuzzles(){
 state.puzzle={fen:'7k/5Q2/6K1/8/8/8/8/8 w - - 0 1',solution:'f7g7'};
 contentView.innerHTML=head('Daily Puzzle','FIND THE WINNING MOVE')+`<div class="paper-card"><h3>Mate in one</h3><p>White to move. Find the one move that delivers checkmate.</p><div class="board-wrap" style="max-width:420px;margin:15px auto"><div class="chess-board" id="chessBoard" role="grid" aria-label="Puzzle board"></div></div><div class="result-banner" id="puzzleMessage">Choose your move.</div><div class="section-actions" style="justify-content:center"><button class="small-btn" data-action="puzzle-reset">Reset puzzle</button><button class="gold-btn" data-action="puzzle-hint">Show hint</button></div><p class="note">This puzzle runs locally in your browser.</p></div>`;
 state.game=new Chess(state.puzzle.fen);state.mode='puzzle';state.playerColor='white';state.selected=null;state.legal=[];state.flipped=false;state.lastMove=null;drawBoard(state.game);
}
function renderInventory(){
 const owned=state.session?.me?'<div class="inventory-row"><div class="inventory-icon">♛</div><div class="desc"><h4>Golden Crown</h4><p>Welcome reward for registered players.</p></div><span>✓</span></div>':'<div class="inventory-row"><div class="inventory-icon">♟</div><div class="desc"><h4>Guest Badge</h4><p>Your starting badge. Sign in online to keep your stats.</p></div><span>✓</span></div>';
 contentView.innerHTML=head('Inventory','YOUR COLLECTION')+`<div class="paper-card"><h3>Chess Studio collection</h3><p>Cosmetics and rewards are collected as you play.</p>${owned}<div class="inventory-row"><div class="inventory-icon">✨</div><div class="desc"><h4>Golden Spark</h4><p>Animated sparkle theme · Preview only</p></div><span>✦</span></div><div class="section-actions"><button class="gold-btn" data-screen="online">Play Online</button><button class="small-btn" data-action="home">Back to menu</button></div></div>`;
}

function renderOnline(){
  if(!state.session?.token){renderAuthForm('login');return;}
  contentView.innerHTML=head('Play Online','CHALLENGE THE WORLD')+`<div class="paper-card"><div style="display:flex;align-items:center;gap:12px"><div class="player-icon" style="width:48px;height:48px;font-size:28px">♞</div><div style="flex:1"><h3 style="margin-bottom:2px">${escapeHtml(state.session.username)}</h3><p class="note" style="margin:0">Signed in to Chess Studio Online</p></div><button class="small-btn" data-action="logout">Sign out</button></div><div class="profile-stats" id="onlineStats" style="margin-top:14px"><div class="stat"><b>—</b><span>Rating</span></div><div class="stat"><b>—</b><span>Games</span></div><div class="stat"><b>—</b><span>Wins</span></div></div></div><div class="paper-card"><h3>Find a match</h3><p>Pick your time control and join the queue. Your opponent and clock are assigned when the server pairs you.</p><div class="choice-row"><button class="choice" data-tc="bullet">1 + 0 · Bullet</button><button class="choice selected" data-tc="blitz">5 + 0 · Blitz</button><button class="choice" data-tc="rapid">10 + 0 · Rapid</button></div><div class="section-actions"><button class="gold-btn" data-action="queue">Find opponent ♟</button><button class="small-btn" data-action="friends">Friends & players</button></div><p class="note">Online accounts, rating and matchmaking are handled by the Chess Studio server connected to this website.</p></div><div class="paper-card hidden" id="matchStatusCard"><h3>Searching for opponent</h3><p><span class="queue-pulse"></span><span id="queueStatus">Joining queue…</span></p><div class="section-actions"><button class="small-btn" data-action="cancel-queue">Cancel search</button></div></div><div class="paper-card" id="friendsCard"><h3>Players</h3><div style="display:flex;gap:7px"><input class="field" id="playerSearch" placeholder="Search username"><button class="small-btn" data-action="search-player">Search</button></div><div id="playerResults"></div></div>`;
 loadOnlineStats();
}
function renderAuthForm(kind='login'){
 state.authMode=kind;
 contentView.innerHTML=head('Play Online','SIGN IN TO PLAY')+`<div class="paper-card"><h3>${kind==='login'?'Welcome back, player':'Create your account'}</h3><p>${kind==='login'?'Sign in to queue for matches, keep your rating, and add friends.':'Choose a username and starting rating bracket.'}</p><form id="authForm"><label class="field-label" for="username">USERNAME</label><input class="field" id="username" name="username" required minlength="3" maxlength="16" autocomplete="username" placeholder="YourChessName" pattern="[A-Za-z0-9_]{3,16}"><label class="field-label" for="password">PASSWORD</label><input class="field" id="password" name="password" type="password" minlength="6" required autocomplete="${kind==='login'?'current-password':'new-password'}" placeholder="At least 6 characters">${kind==='register'?'<label class="field-label" for="bracket">STARTING LEVEL</label><select class="field" id="bracket" name="bracket"><option value="new">New · 200 rating</option><option value="beginner" selected>Beginner · 600 rating</option><option value="intermediate">Intermediate · 1100 rating</option><option value="pro">Pro · 1650 rating</option></select>':''}<button class="gold-btn" style="width:100%;margin-top:15px">${kind==='login'?'Sign in':'Create account'}</button><p class="legal-note">Your account is stored on the Chess Studio server this site connects to. Don't reuse a password from another account.</p></form><div class="section-actions"><button class="small-btn" data-action="auth-toggle">${kind==='login'?'New here? Create account':'Already have an account? Sign in'}</button><button class="small-btn" data-action="home">Back to menu</button></div></div><div class="paper-card"><h3>Play offline instead</h3><p>You can still play the bot, learn piece moves, or try the puzzle without signing in.</p><div class="section-actions"><button class="gold-btn" data-screen="bot">Play vs Bot</button><button class="small-btn" data-screen="basics">Chess Basics</button></div></div>`;
}
async function loadOnlineStats(){
 if(!state.session?.token)return;
 const r=await apiPost('me');if(!r.ok){if(r.auth===false){saveSession(null);renderAuthForm('login');toast('Please sign in again.');}return;}
 state.session.me=r.me;updateAccountChip();const el=$('#onlineStats');if(el)el.innerHTML=`<div class="stat"><b>${r.me.elo}</b><span>Rating</span></div><div class="stat"><b>${r.me.games}</b><span>Games</span></div><div class="stat"><b>${r.me.wins}</b><span>Wins</span></div>`;
}
async function submitAuth(form){
 const f=new FormData(form);const kind=state.authMode;
 const r=await apiPost(kind==='login'?'login':'register',{username:f.get('username'),password:f.get('password'),...(kind==='register'?{bracket:f.get('bracket')}: {})});
 if(!r.ok){toast(r.error||'Could not sign in.');return;}
 saveSession({username:r.me.name,token:r.token,me:r.me});state.sessionMe=r.me;toast(kind==='login'?'Welcome back!':'Account created. Welcome to Chess Studio!');renderOnline();
}
async function startQueue(){
 const r=await apiPost('queue',{tc:state.selectedTime});if(!r.ok){toast(r.error||'Could not join queue.');return;}
 $('#matchStatusCard')?.classList.remove('hidden');$('#queueStatus').textContent='Looking for a player…';state.matchPoll=setInterval(pollMatchStatus,1500);pollMatchStatus();
}
async function pollMatchStatus(){
 const r=await apiPost('status'); if(!r.ok){if(r.auth===false){stopPolling();saveSession(null);renderAuthForm('login');}return;}
 if(r.game){state.onlineGameId=r.game;const gr=await apiPost('game',{id:r.game});if(gr.ok&&gr.game){stopPolling();state.onlineGameId=r.game;state.screen='online-game';state.onlineBoard=new Chess();renderGame('online-game',gr.game);state.gamePoll=setInterval(pollOnlineGame,1100);}return;}
 const stat=$('#queueStatus');if(stat)stat.textContent=`Searching… ${r.players_online||0} player${r.players_online===1?'':'s'} online`;
}
async function pollOnlineGame(){
 if(!state.onlineGameId||state.screen!=='online-game')return;
 const r=await apiPost('game',{id:state.onlineGameId});if(!r.ok){if(r.error==='Game not found'){stopPolling();toast('This game has ended or expired.');return;}return;}
 updateOnlineGameUI(r.game);
}
function updateOnlineGameUI(g){
 if(!g)return;state.lastOnlineGame=g;const u=state.session?.username?.toLowerCase();const mySide=g.you||((g.players?.white?.name||'').toLowerCase()===u?'white':'black');const enemySide=mySide==='white'?'black':'white';
 if($('#topClock'))$('#topClock').textContent=formatClock(g.clock?.[enemySide]);if($('#bottomClock'))$('#bottomClock').textContent=formatClock(g.clock?.[mySide]);
 const status=$('#gameStatus');if(status)status.textContent=g.status==='over'?`${g.result==='1/2-1/2'?'Draw':g.result==='aborted'?'Game aborted':(g.result==='1-0'?'White wins':'Black wins')} · ${g.reason||''}`:g.turn===mySide?'Your move':'Opponent is thinking…';
 const players=contentView.querySelectorAll('.player-strip');if(players.length>=2){players[0].querySelector('span:nth-child(2)').textContent=g.players?.[enemySide]?.name||'Opponent';players[1].querySelector('span:nth-child(2)').textContent=g.players?.[mySide]?.name||'You';}
 const next=new Chess();let last=null;for(const uci of (g.moves||[])){try{const m=next.move({from:uci.slice(0,2),to:uci.slice(2,4),promotion:uci.slice(4)||'q'});last=m?[m.from,m.to]:last;}catch{}}
 state.onlineBoard=next;state.lastMove=last;drawBoard(next);
 const history=next.history();const arr=[];for(let i=0;i<history.length;i+=2)arr.push(`${Math.floor(i/2)+1}. ${history[i]}${history[i+1]?' '+history[i+1]:''}`);if($('#moveList'))$('#moveList').textContent=arr.join('  ')||'No moves yet.';
 const chat=$('#chatLog');if(chat){const lines=g.chat||[];const html=lines.map(m=>`<div><b>${escapeHtml(m.name||m.from)}:</b> ${escapeHtml(m.text)}</div>`).join('');if(chat.dataset.html!==html){chat.innerHTML=html;chat.dataset.html=html;chat.scrollTop=chat.scrollHeight;}}
 if(g.status==='over'&&state.screen==='online-game'){$('#gameStatus').textContent+=` · ${g.reason||''}`;}
}
function formatClock(ms){if(ms==null)return '—';const sec=Math.max(0,Math.floor(ms/1000));return `${Math.floor(sec/60)}:${String(sec%60).padStart(2,'0')}`;}
async function onlineSquare(square){
 const g=state.lastOnlineGame;if(!g||g.status!=='playing'||g.turn!==g.you)return;
 const chess=state.onlineBoard;const p=chess.get(square);
 if(state.selected){let move=null;try{move=chess.move({from:state.selected,to:square,promotion:'q'});}catch{}
  if(move){const uci=move.from+move.to+(move.promotion||'');state.lastMove=[move.from,move.to];state.selected=null;state.legal=[];drawBoard(chess);const r=await apiPost('move',{id:state.onlineGameId,uci});if(!r.ok){toast(r.error||'Move not accepted.');pollOnlineGame();}else updateOnlineGameUI(r.game);return;}}
 if(p&&((p.color==='w'&&g.you==='white')||(p.color==='b'&&g.you==='black'))){state.selected=square;state.legal=chess.moves({square,verbose:true});}else{state.selected=null;state.legal=[];}drawBoard(chess);
}
async function searchPlayers(){const q=$('#playerSearch')?.value.trim();if(!q)return;const r=await apiPost('search',{q});const out=$('#playerResults');if(!out)return;out.innerHTML=!r.ok?`<p class="note">${escapeHtml(r.error)}</p>`:r.results.length?r.results.map(p=>`<div class="inventory-row"><div class="inventory-icon">♟</div><div class="desc"><h4>${escapeHtml(p.name)} <span class="note">· ${p.elo}</span></h4><p>${p.online?'● Online':'○ Offline'} · ${escapeHtml(p.relation)}</p></div></div>`).join(''):'<p class="note">No matching players found.</p>';}

// One delegated event handler keeps all screens lightweight and consistent.
document.addEventListener('click',async e=>{
 const screenBtn=e.target.closest('[data-screen]');if(screenBtn){const s=screenBtn.dataset.screen;if(s==='home')showHome();else screen(s);return;}
 const sq=e.target.closest('[data-square]');if(sq){onSquare(sq.dataset.square);return;}
 const action=e.target.closest('[data-action]')?.dataset.action;
 if(action){
  if(action==='home'){showHome();return;}
  if(action==='auth-toggle'){renderAuthForm(state.authMode==='login'?'register':'login');return;}
  if(action==='start-bot'){startBotGame();return;}
  if(action==='flip'){state.flipped=!state.flipped;drawBoard();return;}
  if(action==='undo'){
    if(state.game&&state.game.history().length){state.game.undo();if(['bot','coach','boss'].includes(state.mode)&&state.game.history().length)state.game.undo();state.selected=null;state.legal=[];state.lastMove=null;drawBoard();updateGameStatus();}return;
  }
  if(action==='new-game'){if(state.screen==='online-game'){toast('Exit to Play Online to find another opponent.');}else if(state.mode==='puzzle'){renderPuzzles();}else if(state.mode==='local'){startLocalGame();}else startBotGame();return;}
  if(action==='puzzle-reset'){renderPuzzles();return;}
  if(action==='puzzle-hint'){toast('Hint: move the queen next to the black king.');return;}
  if(action==='queue'){startQueue();return;}
  if(action==='cancel-queue'){await apiPost('unqueue');stopPolling();$('#matchStatusCard')?.classList.add('hidden');toast('Search cancelled.');return;}
  if(action==='logout'){stopPolling();await apiPost('unqueue').catch(()=>{});saveSession(null);renderAuthForm('login');toast('Signed out.');return;}
  if(action==='search-player'){searchPlayers();return;}
  if(action==='friends'){toast('Search players by username in the Players panel.');$('#playerSearch')?.focus();return;}
  if(action==='offer-draw'){const r=await apiPost('draw',{id:state.onlineGameId,action:'offer'});if(!r.ok)toast(r.error||'Could not offer a draw.');else{toast('Draw offer sent.');updateOnlineGameUI(r.game);}return;}
  if(action==='resign'){if(confirm('Resign this game?')){const r=await apiPost('resign',{id:state.onlineGameId});if(r.ok)updateOnlineGameUI(r.game);}return;}
 }
 const level=e.target.closest('[data-level]');if(level){state.difficulty=level.dataset.level;$$('[data-level]').forEach(b=>b.classList.toggle('selected',b===level));return;}
 const color=e.target.closest('[data-color]');if(color){state.playerColor=color.dataset.color;$$('[data-color]').forEach(b=>b.classList.toggle('selected',b===color));return;}
 const tc=e.target.closest('[data-tc]');if(tc){state.selectedTime=tc.dataset.tc;$$('[data-tc]').forEach(b=>b.classList.toggle('selected',b===tc));return;}
 const lesson=e.target.closest('[data-lesson]');if(lesson){const details=['Start with the pawn. It moves forward, but captures diagonally.','The rook travels in straight lines, horizontally or vertically.','The knight jumps in an L-shape: two squares, then one sideways.','The bishop travels diagonally and stays on its square color.','The queen combines the rook and bishop moves.','The king moves one square in any direction. Never move into check.'];const n=Number(lesson.dataset.lesson);$('#lessonDetail').textContent=details[n];$('#lessonProgress').style.width=`${(n+1)/6*100}%`;return;}
});
document.addEventListener('submit',async e=>{
 if(e.target.id==='authForm'){e.preventDefault();submitAuth(e.target);}
 if(e.target.id==='chatForm'){e.preventDefault();const input=$('#chatMessage');const value=input.value.trim();if(value){input.value='';const r=await apiPost('chat',{id:state.onlineGameId,text:value});if(!r.ok)toast(r.error||'Message not sent.');else updateOnlineGameUI(r.game);}}
});
$('#accountBtn').addEventListener('click',()=>screen('online'));

// Small screen interactions are kept inside the same visual frame.
if(state.session?.token){updateAccountChip();}
