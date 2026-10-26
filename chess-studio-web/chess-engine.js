/* Lightweight, self-contained browser chess rules used by Chess Studio Web.
   Supports legal moves, castling, en passant, promotion, check, mate and draw checks. */
const START_FEN='rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const FILES='abcdefgh';
const PROMOTIONS=['q','r','b','n'];
const KNIGHT_STEPS=[[-2,-1],[-2,1],[-1,-2],[-1,2],[1,-2],[1,2],[2,-1],[2,1]];
const KING_STEPS=[[-1,-1],[-1,0],[-1,1],[0,-1],[0,1],[1,-1],[1,0],[1,1]];
const cloneBoard=b=>b.map(row=>row.map(p=>p?{type:p.type,color:p.color}:null));
const inside=(r,c)=>r>=0&&r<8&&c>=0&&c<8;
const rc=s=>({r:8-Number(s[1]),c:FILES.indexOf(s[0])});
const sq=(r,c)=>FILES[c]+(8-r);
const opposite=c=>c==='w'?'b':'w';

export class Chess {
  constructor(fen=START_FEN){this.load(fen);}
  load(fen=START_FEN){
    const parts=String(fen).trim().split(/\s+/);if(parts.length<4)throw new Error('Invalid FEN');
    const ranks=parts[0].split('/');if(ranks.length!==8)throw new Error('Invalid FEN');
    this._board=Array.from({length:8},()=>Array(8).fill(null));
    ranks.forEach((line,r)=>{let c=0;for(const ch of line){if(/[1-8]/.test(ch))c+=Number(ch);else if(/[prnbqkPRNBQK]/.test(ch)&&c<8){this._board[r][c++]={type:ch.toLowerCase(),color:ch===ch.toUpperCase()?'w':'b'};}else throw new Error('Invalid FEN board');}if(c!==8)throw new Error('Invalid FEN rank');});
    this._turn=parts[1]==='b'?'b':'w';this._castling=parts[2]==='-'?'':parts[2];this._ep=parts[3]==='-'?null:parts[3];
    this._halfmove=Math.max(0,Number(parts[4]||0));this._fullmove=Math.max(1,Number(parts[5]||1));
    this._history=[];this._positions=[this._positionKey()];this._undoStack=[];return true;
  }
  turn(){return this._turn;}
  board(){return cloneBoard(this._board);}
  get(square){if(!/^[a-h][1-8]$/.test(square))return null;const {r,c}=rc(square),p=this._board[r][c];return p?{...p}:null;}
  fen(){
    const ranks=[];for(let r=0;r<8;r++){let out='',empty=0;for(let c=0;c<8;c++){const p=this._board[r][c];if(!p)empty++;else{if(empty){out+=empty;empty=0;}out+=p.color==='w'?p.type.toUpperCase():p.type;}}if(empty)out+=empty;ranks.push(out);}
    return `${ranks.join('/')} ${this._turn} ${this._castling||'-'} ${this._ep||'-'} ${this._halfmove} ${this._fullmove}`;
  }
  _positionKey(){const f=this.fen().split(' ');return f.slice(0,4).join(' ');}
  _snapshot(){return {board:cloneBoard(this._board),turn:this._turn,castling:this._castling,ep:this._ep,halfmove:this._halfmove,fullmove:this._fullmove,history:this._history.slice(),positions:this._positions.slice()};}
  _restore(s){this._board=s.board;this._turn=s.turn;this._castling=s.castling;this._ep=s.ep;this._halfmove=s.halfmove;this._fullmove=s.fullmove;this._history=s.history;this._positions=s.positions;}
  _kingSquare(color){for(let r=0;r<8;r++)for(let c=0;c<8;c++){const p=this._board[r][c];if(p&&p.type==='k'&&p.color===color)return sq(r,c);}return null;}
  _attacked(square,byColor){
    const {r,c}=rc(square);
    // Pawn attacks point diagonally toward the target; locate pawns behind the target.
    const pawnSourceR=r+(byColor==='w'?1:-1);
    for(const dc of [-1,1]){const cc=c+dc;if(inside(pawnSourceR,cc)){const p=this._board[pawnSourceR][cc];if(p&&p.color===byColor&&p.type==='p')return true;}}
    for(const [dr,dc] of KNIGHT_STEPS){const rr=r+dr,cc=c+dc;if(inside(rr,cc)){const p=this._board[rr][cc];if(p&&p.color===byColor&&p.type==='n')return true;}}
    for(const [dr,dc] of KING_STEPS){const rr=r+dr,cc=c+dc;if(inside(rr,cc)){const p=this._board[rr][cc];if(p&&p.color===byColor&&p.type==='k')return true;}}
    const rays=[[-1,0],[1,0],[0,-1],[0,1],[-1,-1],[-1,1],[1,-1],[1,1]];
    for(let i=0;i<rays.length;i++){const [dr,dc]=rays[i];let rr=r+dr,cc=c+dc,dist=1;while(inside(rr,cc)){const p=this._board[rr][cc];if(p){if(p.color===byColor&&(p.type==='q'||(i<4&&p.type==='r')||(i>=4&&p.type==='b')||(dist===1&&p.type==='k')))return true;break;}rr+=dr;cc+=dc;dist++;}}
    return false;
  }
  _inCheck(color=this._turn){const k=this._kingSquare(color);return k?this._attacked(k,opposite(color)):false;}
  isCheck(){return this._inCheck(this._turn);}
  _pseudoMoves(filterSquare=null){
    const out=[];const add=(from,to,p,extra={})=>out.push({from,to,color:p.color,piece:p.type,...extra});
    for(let r=0;r<8;r++)for(let c=0;c<8;c++){
      const p=this._board[r][c];if(!p||p.color!==this._turn)continue;const from=sq(r,c);if(filterSquare&&from!==filterSquare)continue;
      if(p.type==='p'){
        const dr=p.color==='w'?-1:1,start=p.color==='w'?6:1,last=p.color==='w'?0:7;const nr=r+dr;
        if(inside(nr,c)&&!this._board[nr][c]){if(nr===last)for(const promotion of PROMOTIONS)add(from,sq(nr,c),p,{promotion,flags:'p'});else add(from,sq(nr,c),p,{flags:'n'});
          const nr2=r+2*dr;if(r===start&&!this._board[nr2][c])add(from,sq(nr2,c),p,{flags:'b'});}
        for(const dc of [-1,1]){const cc=c+dc;if(!inside(nr,cc))continue;const target=this._board[nr][cc];if(target&&target.color!==p.color&&target.type!=='k'){if(nr===last)for(const promotion of PROMOTIONS)add(from,sq(nr,cc),p,{promotion,captured:target.type,flags:'cp'});else add(from,sq(nr,cc),p,{captured:target.type,flags:'c'});}else if(this._ep===sq(nr,cc)){add(from,sq(nr,cc),p,{captured:'p',flags:'e'});}}
      }else if(p.type==='n'){
        for(const [dr,dc] of KNIGHT_STEPS){const rr=r+dr,cc=c+dc;if(!inside(rr,cc))continue;const target=this._board[rr][cc];if(!target)add(from,sq(rr,cc),p,{flags:'n'});else if(target.color!==p.color&&target.type!=='k')add(from,sq(rr,cc),p,{captured:target.type,flags:'c'});}
      }else{
        let dirs=p.type==='k'?KING_STEPS:p.type==='b'?KING_STEPS.filter(([dr,dc])=>dr!==0&&dc!==0):p.type==='r'?KING_STEPS.filter(([dr,dc])=>dr===0||dc===0):KING_STEPS;
        for(const [dr,dc] of dirs){let rr=r+dr,cc=c+dc;while(inside(rr,cc)){const target=this._board[rr][cc];if(!target)add(from,sq(rr,cc),p,{flags:'n'});else{if(target.color!==p.color&&target.type!=='k')add(from,sq(rr,cc),p,{captured:target.type,flags:'c'});break;}if(p.type==='k')break;rr+=dr;cc+=dc;}}
        if(p.type==='k'&&from===(p.color==='w'?'e1':'e8')&&!this._inCheck(p.color)){
          const back=p.color==='w'?7:0, enemy=opposite(p.color), rights=p.color==='w'?['K','Q']:['k','q'];
          if(this._castling.includes(rights[0])&&!this._board[back][5]&&!this._board[back][6]&&!this._attacked(sq(back,5),enemy)&&!this._attacked(sq(back,6),enemy))add(from,sq(back,6),p,{flags:'k'});
          if(this._castling.includes(rights[1])&&!this._board[back][1]&&!this._board[back][2]&&!this._board[back][3]&&!this._attacked(sq(back,3),enemy)&&!this._attacked(sq(back,2),enemy))add(from,sq(back,2),p,{flags:'q'});
        }
      }
    }
    return out;
  }
  _apply(move){
    const a=rc(move.from),b=rc(move.to),p=this._board[a.r][a.c];let captured=this._board[b.r][b.c];
    if(p.type==='p'&&move.flags==='e'){captured=this._board[a.r][b.c];this._board[a.r][b.c]=null;}
    this._board[a.r][a.c]=null;this._board[b.r][b.c]=p?{...p}:null;
    if(p&&p.type==='p'&&(b.r===0||b.r===7))this._board[b.r][b.c]={type:move.promotion||'q',color:p.color};
    if(p&&p.type==='k'&&Math.abs(b.c-a.c)===2){if(b.c===6){this._board[b.r][5]=this._board[b.r][7];this._board[b.r][7]=null;}else{this._board[b.r][3]=this._board[b.r][0];this._board[b.r][0]=null;}}
    let rights=this._castling;if(p&&p.type==='k')rights=rights.replace(p.color==='w'?/[KQ]/g:/[kq]/g,'');
    if(move.from==='a1'||move.to==='a1')rights=rights.replace('Q','');if(move.from==='h1'||move.to==='h1')rights=rights.replace('K','');if(move.from==='a8'||move.to==='a8')rights=rights.replace('q','');if(move.from==='h8'||move.to==='h8')rights=rights.replace('k','');this._castling=rights;
    this._ep=p&&p.type==='p'&&Math.abs(b.r-a.r)===2?sq((a.r+b.r)/2,a.c):null;
    this._halfmove=p&&p.type==='p'||captured?0:this._halfmove+1;if(this._turn==='b')this._fullmove++;this._turn=opposite(this._turn);
  }
  _legalMoves(filterSquare=null){const out=[];for(const m of this._pseudoMoves(filterSquare)){const snap=this._snapshot();this._apply(m);const ok=!this._inCheck(m.color);this._restore(snap);if(ok){const a=rc(m.from),b=rc(m.to);const target=this._board[b.r][b.c];out.push({...m,captured:m.captured||(target&&target.color!==m.color?target.type:undefined),san:''});}}return out;}
  moves(options={}){const all=this._legalMoves(options.square||null);return options.verbose?all:all.map(m=>m.from+m.to+(m.promotion||''));}
  move(input){
    if(!input||typeof input!=='object'||!input.from||!input.to)throw new Error('Invalid move');
    const candidate=this._legalMoves(input.from).find(m=>m.to===input.to&&(!m.promotion||m.promotion===(input.promotion||'q')));
    if(!candidate)throw new Error('Illegal move');
    const snap=this._snapshot();const a=rc(candidate.from),b=rc(candidate.to);const piece=this._board[a.r][a.c];
    let san='';if(candidate.flags==='k')san='O-O';else if(candidate.flags==='q')san='O-O-O';else{
      const takes=!!candidate.captured||candidate.flags==='e';if(piece.type!=='p')san=piece.type.toUpperCase();else if(takes)san=candidate.from[0];
      san+=(takes?'x':'')+candidate.to;if(candidate.promotion)san+='='+candidate.promotion.toUpperCase();
    }
    this._undoStack.push(snap);this._apply(candidate);if(this.isCheckmate())san+='#';else if(this.isCheck())san+='+';
    const record={...candidate,san};this._history.push(record);this._positions.push(this._positionKey());return {...record};
  }
  undo(){if(!this._undoStack.length)return null;const last=this._history[this._history.length-1];this._restore(this._undoStack.pop());return last?{...last}:null;}
  history(options={}){return options.verbose?this._history.map(m=>({...m})):this._history.map(m=>m.san);}
  isCheckmate(){return this._inCheck(this._turn)&&this._legalMoves().length===0;}
  isStalemate(){return !this._inCheck(this._turn)&&this._legalMoves().length===0;}
  isInsufficientMaterial(){const pieces=[];for(let r=0;r<8;r++)for(let c=0;c<8;c++){const p=this._board[r][c];if(p&&p.type!=='k')pieces.push({ ...p, r,c});}if(!pieces.length)return true;if(pieces.some(p=>['p','q','r'].includes(p.type)))return false;if(pieces.length===1)return true;if(pieces.every(p=>p.type==='b')){const colors=new Set(pieces.map(p=>(p.r+p.c)%2));return colors.size===1;}return false;}
  isDraw(){return this.isStalemate()||this.isInsufficientMaterial()||this._halfmove>=100||this._positions.filter(k=>k===this._positionKey()).length>=3;}
  isGameOver(){return this.isCheckmate()||this.isDraw();}
}
