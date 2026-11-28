// ---- WebGL renderer ----
const FACES_N = [3, 4, 1, 6, 2, 5];       // value per body axis [+x,-x,+y,-y,+z,-z]
const FACES_L = [5, 5, 4, 4, 6, 6];       // loaded 4-5-6 dice
const GFX = (() => {
  let gl, cv, prog, U = {}, A = {}, M = {}, texN, texL, dpr = 1, W = 1, H = 1, ok = false;
  const cam = { az: 0, el: 0.95, dist: 17, tx: 0, ty: 0.9, tz: -0.2, // current
    goal: { az: 0, el: 0.95, dist: 17, tx: 0, ty: 0.9, tz: -0.2 }, user: { az: 0, el: 0.95, zoom: 1 } };
  let shake = 0;
  const VS = `attribute vec3 aP;attribute vec3 aN;attribute vec2 aUV;attribute float aS;
uniform mat4 uVP;uniform mat4 uM;varying vec3 vP;varying vec3 vN;varying vec2 vUV;varying float vS;varying vec3 vL;
void main(){vec4 w=uM*vec4(aP,1.);vP=w.xyz;vN=mat3(uM)*aN;vUV=aUV;vS=aS;vL=aP;gl_Position=uVP*w;}`;
  const FS = `precision highp float;
uniform float uMat;uniform sampler2D uTex;uniform vec3 uCam;uniform vec3 uLt;uniform vec3 uD[3];uniform float uSelf;
uniform vec4 uBowl;uniform float uRimO;uniform float uTime;uniform float uTab;
varying vec3 vP;varying vec3 vN;varying vec2 vUV;varying float vS;varying vec3 vL;
float h21(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
float vn(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);return mix(mix(h21(i),h21(i+vec2(1,0)),f.x),mix(h21(i+vec2(0,1)),h21(i+vec2(1,1)),f.x),f.y);}
float fbm(vec2 p){float s=0.,a=.5;for(int i=0;i<5;i++){s+=a*vn(p);p*=2.03;a*=.5;}return s;}
float sphSh(vec3 p,vec3 l){float s=1.;for(int i=0;i<3;i++){if(float(i)==uSelf)continue;vec3 oc=uD[i]-p;float t=dot(oc,l);if(t>0.){float d=length(oc-l*t);s*=mix(.3,1.,smoothstep(.3,.62+.045*t,d));}}return s;}
float aoD(vec3 p,vec3 n){float a=1.;for(int i=0;i<3;i++){if(float(i)==uSelf)continue;vec3 d=uD[i]-p;float l=length(d);a*=1.-.6*clamp(dot(n,d/l)*.6+.4,0.,1.)*clamp(pow(.72/l,3.),0.,1.);}return a;}
float bowlSh(vec3 p,vec3 l){
 float R=uBowl.x,CY=uBowl.y,RIM=uBowl.z,RR=uBowl.w;vec3 c=vec3(0,CY,0);
 if(length(p.xz)<RR+.1&&p.y<RIM+.05&&length(p-c)<R+.05){float t=(RIM-p.y)/max(l.y,.01);vec2 q=p.xz+l.xz*t;return smoothstep(.45,-.35,length(q)-RR);}
 vec3 oc=p-c;float b=dot(oc,l);float cc=dot(oc,oc)-(R+.3)*(R+.3);float hh=b*b-cc;
 if(b<0.&&hh>0.){float t=-b-sqrt(hh);vec3 q=p+l*t;if(q.y<RIM){float dd=length(oc-l*dot(oc,l));return mix(.25,1.,smoothstep(R+.3-1.2,R+.3,dd)*.7);}}
 return 1.;}
vec3 tone(vec3 c){c=1.-exp(-c*1.25);return pow(c,vec3(1./2.2));}
void main(){
 vec3 n=normalize(vN);vec3 v=normalize(uCam-vP);vec3 L=uLt-vP;float ld=length(L);vec3 l=L/ld;vec3 hv=normalize(l+v);
 float spot=smoothstep(.55,.95,dot(-l,normalize(vec3(-.33,-.88,-.38))));
 float sh=sphSh(vP+n*.02,l)*bowlSh(vP+n*.03,l);
 float ao=aoD(vP,n);
 vec3 lc=vec3(1.,.85,.64)*1.85*spot;vec3 amb=vec3(.12,.12,.16);
 vec3 col;float ndl=max(dot(n,l),0.);float fr=pow(1.-max(dot(n,v),0.),5.);
 if(uMat<.5){ // die
  vec4 t=texture2D(uTex,vUV);vec3 base=pow(t.rgb,vec3(2.2));float pip=1.-smoothstep(.25,.6,dot(t.rgb,vec3(.33)));
  float wrap=max((dot(n,l)+.25)/1.25,0.);
  float sp=pow(max(dot(n,hv),0.),90.)*(1.-pip*.7)*1.3;
  vec3 env=mix(vec3(.05,.04,.04),vec3(.5,.42,.32),n.y*.5+.5);
  col=base*(lc*wrap*sh+amb*1.2)*ao+lc*sp*sh+env*fr*.35+base*vec3(.05,.03,.02);
 } else if(uMat<1.5){ // bowl
  float s=vS;vec3 base;float gl=1.;float ex=140.;
  float ang=atan(vP.z,vP.x);
  if(s<.5){ // inner glaze
   base=vec3(.74,.72,.66);gl=.55;ex=220.;float cr=smoothstep(.015,0.,abs(vn(vP.xz*3.1+vec2(ang))-.5))*.025;base-=cr;
   float tt=vUV.y;base=mix(base,vec3(.09,.16,.38),smoothstep(.905,.915,tt)*(1.-smoothstep(.935,.945,tt)));
   base=mix(base,vec3(.09,.16,.38),smoothstep(.955,.96,tt));
  } else if(s<1.5){ // outer indigo with seigaiha
   vec2 g=vec2(ang/6.2832*26.,vP.y*2.6);
   float best=1e3;float dmin=0.;
   for(int j=-2;j<=2;j++)for(int i=-1;i<=1;i++){float jy=floor(g.y*2.)+float(j);vec2 cpos=vec2(floor(g.x)+float(i)+.5+.5*mod(jy,2.),jy*.5);float d=length(g-cpos);if(d<.62&&cpos.y<best){best=cpos.y;dmin=d;}}
   float rings=smoothstep(.12,.0,abs(fract(dmin*4.6)-.5)-.18);
   base=mix(vec3(.04,.07,.2),vec3(.8,.82,.85),rings*.8);
   float band=smoothstep(.03,0.,abs(vP.y-2.25))+smoothstep(.03,0.,abs(vP.y-.6));base=mix(base,vec3(.85,.86,.88),band);
   base*=.9+.2*vn(g*3.);
  } else if(s<2.5){base=vec3(.42,.24,.12);ex=90.;}
  else {base=vec3(.55,.45,.34)*(.8+.3*fbm(vec2(ang*6.,vP.y*8.)));gl=.1;ex=12.;}
  base=pow(base,vec3(1.6));
  float sp=pow(max(dot(n,hv),0.),ex)*2.2*gl;
  vec3 env=mix(vec3(.02,.02,.03),vec3(.6,.5,.38),smoothstep(-.2,1.,reflect(-v,n).y));
  float cav=s<.5?mix(.55,1.,smoothstep(.2,2.6,vP.y)):1.;
  col=base*(lc*ndl*sh+amb*cav)*ao+lc*sp*sh+(env*fr*gl*.9+env*.05*gl)*cav;
 } else { // table: dark walnut
  float wv=fbm(vP.xz*vec2(.08,.5))*2.5;float gr=fbm(vec2(vP.x*.22,vP.z*3.2+wv));float ln=smoothstep(.0,.08,abs(fract(vP.z*1.4+wv*1.3+gr)-.5)-.4)*.35;
  vec3 base=mix(vec3(.13,.065,.035),vec3(.27,.15,.075),gr)*(1.-ln);base=pow(base,vec3(1.6));
  float sp=pow(max(dot(n,hv),0.),40.)*.45;
  float d=length(vP.xz);float bao=mix(.35,1.,smoothstep(2.,3.8,d));
  col=base*(lc*ndl*sh+amb*.6)*ao*bao+lc*sp*sh*ao;
  col*=1.-smoothstep(10.,26.,length(vP.xz));
 }
 gl_FragColor=vec4(tone(col),1.);}`;

  // ---- matrices ----
  const m4 = {
    mul(a, b) { const o = new Float32Array(16); for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) { let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = s; } return o; },
    persp(f, a, n, fr) { const t = 1 / Math.tan(f / 2), o = new Float32Array(16); o[0] = t / a; o[5] = t; o[10] = (fr + n) / (n - fr); o[11] = -1; o[14] = 2 * fr * n / (n - fr); return o; },
    look(e, c, u) {
      let z = [e[0] - c[0], e[1] - c[1], e[2] - c[2]]; let l = Math.hypot(...z); z = z.map(x => x / l);
      let x = [u[1] * z[2] - u[2] * z[1], u[2] * z[0] - u[0] * z[2], u[0] * z[1] - u[1] * z[0]]; l = Math.hypot(...x); x = x.map(v => v / l);
      const y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]];
      return new Float32Array([x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0, -(x[0] * e[0] + x[1] * e[1] + x[2] * e[2]), -(y[0] * e[0] + y[1] * e[1] + y[2] * e[2]), -(z[0] * e[0] + z[1] * e[1] + z[2] * e[2]), 1]);
    },
    rt(q, p) {
      const [w, x, y, z] = q;
      return new Float32Array([1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y), 0, 2 * (x * y - w * z), 1 - 2 * (x * x + z * z), 2 * (y * z + w * x), 0, 2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y), 0, p[0], p[1], p[2], 1]);
    },
    id() { return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]); }
  };

  function mesh(P, N, UV, S, I) {
    const d = new Float32Array(P.length / 3 * 9);
    for (let i = 0; i < P.length / 3; i++) { d.set([P[i * 3], P[i * 3 + 1], P[i * 3 + 2], N[i * 3], N[i * 3 + 1], N[i * 3 + 2], UV[i * 2], UV[i * 2 + 1], S[i]], i * 9); }
    const vb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, vb); gl.bufferData(gl.ARRAY_BUFFER, d, gl.STATIC_DRAW);
    const ib = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
    const big = P.length / 3 > 65535; const ext = big && gl.getExtension('OES_element_index_uint');
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, ext ? new Uint32Array(I) : new Uint16Array(I), gl.STATIC_DRAW);
    return { vb, ib, n: I.length, t: ext ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT };
  }
  function dieMesh() {
    const P = [], N = [], UV = [], S = [], I = []; const h = PHYS.H, r = PHYS.SR;
    const ts = [-.5, -.49, -.475, -.455, -.43, -.36, -.24, -.12, 0, .12, .24, .36, .43, .455, .475, .49, .5];
    const AX = PHYS.AX;
    for (let f = 0; f < 6; f++) {
      const nrm = AX[f]; const a = Math.floor(f / 2);
      let t1 = [0, 0, 0], t2 = [0, 0, 0]; t1[(a + 1) % 3] = 1; t2[(a + 2) % 3] = 1;
      if (f % 2) t1 = t1.map(x => -x);
      const base = P.length / 3, n = ts.length;
      for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
        const u = ts[i], w = ts[j];
        const p = [0, 1, 2].map(k => nrm[k] * .5 + t1[k] * u + t2[k] * w);
        const inn = p.map(x => Math.max(-h, Math.min(h, x)));
        let d = p.map((x, k) => x - inn[k]); const l = Math.hypot(...d); d = d.map(x => x / l);
        P.push(...inn.map((x, k) => x + d[k] * r)); N.push(...d); UV.push((f + (u + .5) * .96 + .02) / 6, (w + .5) * .96 + .02); S.push(0);
      }
      for (let j = 0; j < n - 1; j++) for (let i = 0; i < n - 1; i++) {
        const k = base + j * n + i;
        if (f % 2) I.push(k, k + n, k + 1, k + 1, k + n, k + n + 1); else I.push(k, k + 1, k + n, k + 1, k + n + 1, k + n);
      }
    }
    return mesh(P, N, UV, S, I);
  }
  function bowlMesh() {
    const { R, T, CY, RIM, TAB, FOOT } = PHYS; const prof = [];
    const pIn = Math.acos((CY - RIM) / R), Ro = R + T, pOut = Math.acos((CY - RIM) / Ro), pFoot = Math.asin(FOOT / Ro);
    for (let i = 0; i <= 40; i++) { const p = pIn * i / 40; prof.push([R * Math.sin(p), CY - R * Math.cos(p), -Math.sin(p), Math.cos(p), 0, i / 40]); }
    const ri = R * Math.sin(pIn), ro = Ro * Math.sin(pOut), rc = (ri + ro) / 2, rr = (ro - ri) / 2;
    prof.push(null);
    for (let i = 0; i <= 10; i++) { const a = Math.PI - Math.PI * i / 10; prof.push([rc + Math.cos(a) * rr, RIM + Math.sin(a) * rr * 1.2, Math.cos(a), Math.sin(a), 2, 0]); }
    prof.push(null);
    for (let i = 0; i <= 30; i++) { const p = pOut - (pOut - pFoot) * i / 30; prof.push([Ro * Math.sin(p), CY - Ro * Math.cos(p), Math.sin(p), -Math.cos(p), 1, 0]); }
    prof.push(null);
    const yf = CY - Ro * Math.cos(pFoot);
    prof.push([FOOT, yf, 1, 0, 3, 0], [FOOT, TAB, 1, 0, 3, 0], null, [FOOT, TAB, 0, -1, 3, 0], [FOOT - .3, TAB, 0, -1, 3, 0]);
    const P = [], N = [], UV = [], S = [], I = []; const SEG = 120;
    let strip = [];
    const flush = () => {
      if (strip.length < 2) { strip = []; return; }
      const base = P.length / 3;
      for (const q of strip) for (let s = 0; s <= SEG; s++) {
        const a = s / SEG * Math.PI * 2, c = Math.cos(a), sn = Math.sin(a);
        P.push(q[0] * c, q[1], q[0] * sn); N.push(q[2] * c, q[3], q[2] * sn); UV.push(s / SEG, q[5]); S.push(q[4]);
      }
      for (let k = 0; k < strip.length - 1; k++) for (let s = 0; s < SEG; s++) {
        const i0 = base + k * (SEG + 1) + s, i1 = i0 + SEG + 1; I.push(i0, i1, i0 + 1, i0 + 1, i1, i1 + 1);
      }
      strip = [];
    };
    for (const q of prof) { if (q) strip.push(q); else flush(); } flush();
    return mesh(P, N, UV, S, I);
  }
  function quadMesh() {
    const y = PHYS.TAB, s = 40;
    return mesh([-s, y, -s, s, y, -s, s, y, s, -s, y, s], [0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0], [0, 0, 1, 0, 1, 1, 0, 1], [0, 0, 0, 0], [0, 2, 1, 0, 3, 2]);
  }
  const PIPS = { 1: [[0, 0]], 2: [[-1, -1], [1, 1]], 3: [[-1, -1], [0, 0], [1, 1]], 4: [[-1, -1], [1, -1], [-1, 1], [1, 1]], 5: [[-1, -1], [1, -1], [0, 0], [-1, 1], [1, 1]], 6: [[-1, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [1, 1]] };
  function dieTex(faces) {
    const S = 256, c = document.createElement('canvas'); c.width = S * 6; c.height = S; const x = c.getContext('2d');
    for (let f = 0; f < 6; f++) {
      const ox = f * S;
      const g = x.createRadialGradient(ox + S * .4, S * .35, 10, ox + S / 2, S / 2, S * .8); g.addColorStop(0, '#fbf6ea'); g.addColorStop(1, '#e9dfcb');
      x.fillStyle = g; x.fillRect(ox, 0, S, S);
      for (let i = 0; i < 300; i++) { x.fillStyle = `rgba(120,90,50,${Math.random() * .04})`; x.fillRect(ox + Math.random() * S, Math.random() * S, 2, 2); }
      const v = faces[f], one = v === 1, pr = one ? S * .16 : S * .085, sp = S * .25;
      for (const [px, py] of PIPS[v]) {
        const cx = ox + S / 2 + px * sp, cy = S / 2 + py * sp;
        x.beginPath(); x.arc(cx, cy, pr * 1.08, 0, 7); x.fillStyle = 'rgba(90,70,50,.35)'; x.fill();
        const gg = x.createRadialGradient(cx - pr * .25, cy - pr * .3, pr * .1, cx, cy, pr);
        if (one) { gg.addColorStop(0, '#8e0f0c'); gg.addColorStop(.75, '#c4231b'); gg.addColorStop(1, '#5a0907'); }
        else { gg.addColorStop(0, '#000'); gg.addColorStop(.8, '#1d1a18'); gg.addColorStop(1, '#3a3330'); }
        x.beginPath(); x.arc(cx, cy, pr, 0, 7); x.fillStyle = gg; x.fill();
      }
    }
    const t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, c);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }
  function init(canvas) {
    cv = canvas;
    gl = cv.getContext('webgl', { antialias: true, alpha: false }) || cv.getContext('experimental-webgl');
    if (!gl) return false;
    const sh = (t, s) => { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o); if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(o)); return o; };
    prog = gl.createProgram(); gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    for (const n of ['uVP', 'uM', 'uMat', 'uTex', 'uCam', 'uLt', 'uD', 'uSelf', 'uBowl', 'uTime']) U[n] = gl.getUniformLocation(prog, n);
    for (const n of ['aP', 'aN', 'aUV', 'aS']) A[n] = gl.getAttribLocation(prog, n);
    M.die = dieMesh(); M.bowl = bowlMesh(); M.table = quadMesh();
    texN = dieTex(FACES_N); texL = dieTex(FACES_L);
    gl.enable(gl.DEPTH_TEST);
    ok = true; resize(); return true;
  }
  function resize() {
    if (!ok) return;
    dpr = Math.min(window.devicePixelRatio || 1, 1.75);
    W = cv.clientWidth; H = cv.clientHeight; cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    gl.viewport(0, 0, cv.width, cv.height);
  }
  function draw(m) {
    gl.bindBuffer(gl.ARRAY_BUFFER, m.vb); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, m.ib);
    const st = 36; gl.enableVertexAttribArray(A.aP); gl.vertexAttribPointer(A.aP, 3, gl.FLOAT, false, st, 0);
    gl.enableVertexAttribArray(A.aN); gl.vertexAttribPointer(A.aN, 3, gl.FLOAT, false, st, 12);
    gl.enableVertexAttribArray(A.aUV); gl.vertexAttribPointer(A.aUV, 2, gl.FLOAT, false, st, 24);
    gl.enableVertexAttribArray(A.aS); gl.vertexAttribPointer(A.aS, 1, gl.FLOAT, false, st, 32);
    gl.drawElements(gl.TRIANGLES, m.n, m.t, 0);
  }
  function eye() {
    const c = cam; return [c.tx + Math.sin(c.az) * Math.cos(c.el) * c.dist, c.ty + Math.sin(c.el) * c.dist, c.tz + Math.cos(c.az) * Math.cos(c.el) * c.dist];
  }
  function frame(dice, dt, time) {
    if (!ok) return;
    if (cv.clientWidth !== W || cv.clientHeight !== H) resize();
    const k = 1 - Math.exp(-dt * 4);
    for (const key of ['az', 'el', 'dist', 'tx', 'ty', 'tz']) cam[key] += (cam.goal[key] - cam[key]) * k;
    const e = eye();
    if (shake > 0) { e[0] += (Math.random() - .5) * shake; e[1] += (Math.random() - .5) * shake; shake *= Math.exp(-dt * 6); if (shake < .01) shake = 0; }
    const asp = W / H;
    const VP = m4.mul(m4.persp(.62, asp, .5, 120), m4.look(e, [cam.tx, cam.ty, cam.tz], [0, 1, 0]));
    gl.clearColor(.02, .02, .03, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.uniformMatrix4fv(U.uVP, false, VP); gl.uniform3fv(U.uCam, e); gl.uniform3f(U.uLt, 6.5, 18, 8);
    const dp = []; for (let i = 0; i < 3; i++) { const d = dice[i]; dp.push(d ? d.p[0] : 0, d ? d.p[1] : -50, d ? d.p[2] : 0); }
    gl.uniform3fv(U.uD, dp); gl.uniform4f(U.uBowl, PHYS.R, PHYS.CY, PHYS.RIM, PHYS.RIMR); gl.uniform1f(U.uTime, time);
    gl.uniform1f(U.uSelf, -1);
    gl.uniformMatrix4fv(U.uM, false, m4.id());
    gl.uniform1f(U.uMat, 2); draw(M.table);
    gl.uniform1f(U.uMat, 1); draw(M.bowl);
    gl.uniform1f(U.uMat, 0); gl.activeTexture(gl.TEXTURE0); gl.uniform1i(U.uTex, 0);
    dice.forEach((d, i) => {
      if (!d) return;
      gl.bindTexture(gl.TEXTURE_2D, d.loaded ? texL : texN);
      gl.uniform1f(U.uSelf, i);
      gl.uniformMatrix4fv(U.uM, false, m4.rt(PHYS.qmul(d.q, d.o || [1, 0, 0, 0]), d.p)); draw(M.die);
    });
  }
  function project(p) { // world -> css px (approx with current cam)
    const e = eye(); const VP = m4.mul(m4.persp(.62, W / H, .5, 120), m4.look(e, [cam.tx, cam.ty, cam.tz], [0, 1, 0]));
    const x = VP[0] * p[0] + VP[4] * p[1] + VP[8] * p[2] + VP[12], y = VP[1] * p[0] + VP[5] * p[1] + VP[9] * p[2] + VP[13], w = VP[3] * p[0] + VP[7] * p[1] + VP[11] * p[2] + VP[15];
    return [(x / w * .5 + .5) * W, (1 - (y / w * .5 + .5)) * H];
  }
  function baseView() { // fit bowl to screen
    const asp = (W || 1) / (H || 1);
    let d = 16.5; if (asp < 1.05) d = 16.5 * Math.min(2.1, 1.1 / asp);
    return d;
  }
  function setView(v) {
    const g = cam.goal, b = baseView() * cam.user.zoom;
    // 縦長の画面（スマホ）: 上の座席札と下の操作盤のあいだに丼が来るよう、見る点を手前へずらし、少し寄る
    const tall = (W || 1) / (H || 1) < .8, lift = tall ? 2.7 : 0, lx = Math.sin(cam.user.az) * lift, lz = Math.cos(cam.user.az) * lift;
    Object.assign(g, { tx: lx, ty: .9, tz: -.2 + lz, az: cam.user.az, el: cam.user.el, dist: b * (tall ? .94 : 1) });
    if (v && v.focus) { g.tx = v.focus[0]; g.ty = v.focus[1]; g.tz = v.focus[2]; g.dist = b * (v.k || .55); }
    if (v && v.hand) { g.ty = 1.6; g.tx = lx; g.tz = .6 + lz; g.dist = b * (tall ? .96 : 1.02); }
    if (v && v.title) { g.el = .62; g.ty = -2.6; g.dist = b * .95; }
  }
  return { init, frame, resize, setView, cam, project, get ok() { return ok; }, shake(v) { shake = Math.max(shake, v); }, baseView };
})();
