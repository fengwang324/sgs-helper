(function () {
  "use strict";
  var data = window.SGS_CATALOG, model = window.SGS_MODEL.createModel(data);
  var main = document.getElementById("main-content"), dialog = document.getElementById("reference-dialog");
  var dialogContent = document.getElementById("reference-content");
  var popover = null;
  var labels = {general:"武将",skill:"技能",card:"卡牌",faq:"FAQ",keyword:"关键词",product:"产品"};
  var modes = {guozhan:"国战",identity:"身份局",common:"通用",ol:"OL",mobile:"移动版",other:"其他"};
  var sections = [
    ["generals","武将",data.generals.length],["skills","技能",data.skills.length],
    ["cards","游戏牌",data.cards.filter(isGame).length],["markers","指示物",data.cards.filter(function(c){return !isGame(c);}).length],
    ["keywords","关键词",data.keywords.length],["faq","FAQ",data.faq.length],["products","产品",data.products.length]
  ];
  function isGame(c){return c.category_1 === "游戏牌";}
  function el(tag,cls,text){var n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
  function add(parent){Array.prototype.slice.call(arguments,1).forEach(function(n){if(n)parent.append(n);});return parent;}
  function link(text,url,cls){var a=el("a",cls,text);a.href=url;return a;}
  function route(kind,id){return "#/"+kind+"/"+encodeURIComponent(id);}
  function button(text,fn,cls){var b=el("button",cls||"button",text);b.type="button";b.addEventListener("click",fn);return b;}
  function factionMarks(value, cls) { var wrap=el("span",cls||"faction-marks");model.factionParts(value).forEach(function(name){var file=name==="野心家"?"野":name;var img=el("img","faction-image");img.src="web/assets/factions/"+encodeURIComponent(file)+".png";img.alt=name;img.title=name;img.loading="lazy";wrap.append(img);});return wrap; }
  function keywordRows(item) {
    return model.keywordDefinitionRows(item,data.keywords);
  }
  function showKeywordBubble(trigger,item,label) {
    var host=el("span","keyword-popover");host.append(trigger);var bubble=el("span","keyword-bubble");bubble.setAttribute("role","status");bubble.hidden=true;bubble.append(el("strong","",label||item.name));
    var rows=keywordRows(item);if(item.id==="keyword_104"&&label)rows=rows.filter(function(row){return row.indexOf(label+"：")===0;});rows.forEach(function(row){bubble.append(el("span","keyword-definition",row));});host.append(bubble);trigger.setAttribute("aria-expanded","false");trigger.setAttribute("aria-haspopup","true");trigger.addEventListener("click",function(e){e.preventDefault();e.stopPropagation();if(popover&&popover.host===host){bubble.hidden=true;trigger.setAttribute("aria-expanded","false");popover=null;}else{if(popover){popover.bubble.hidden=true;popover.trigger.setAttribute("aria-expanded","false");}popover={host:host,bubble:bubble,trigger:trigger};bubble.hidden=false;trigger.setAttribute("aria-expanded","true");}});return host;
  }
  function suitIcon(char){var base={"❤":"♥","♡":"♥","♢":"♦","♤":"♠","♧":"♣"}[char]||char;var svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 16 16");svg.setAttribute("role","img");svg.setAttribute("aria-label",base==="♥"?"红桃":base==="♦"?"方块":base==="♠"?"黑桃":"梅花");svg.classList.add("suit-icon",base==="♥"||base==="♦"?"red-suit":"black-suit");if(char==="♡"||char==="♢"||char==="♤"||char==="♧")svg.classList.add("outline-suit");var path=document.createElementNS("http://www.w3.org/2000/svg","path");
    var shapes={"♥":"M8 14C6 11.8 1.5 8.5 1.5 5.3A3.2 3.2 0 0 1 8 4a3.2 3.2 0 0 1 6.5 1.3C14.5 8.5 10 11.8 8 14Z","♦":"M8 1 14 8 8 15 2 8Z","♠":"M8 1C6 4 1.5 6.2 1.5 9.1A3.1 3.1 0 0 0 7 11l-1 3h4l-1-3a3.1 3.1 0 0 0 5.5-1.9C14.5 6.2 10 4 8 1Z","♣":"M8 1a3 3 0 0 0-2 5.2A3 3 0 1 0 9 11l-1 3h4l-1-3a3 3 0 1 0-1-4.8A3 3 0 0 0 8 1Z"};path.setAttribute("d",shapes[base]);svg.append(path);return svg;
  }
  function suitPositions(text){return model.cardSuitTokens(text);}
  function keywordPositions(text){var out=[],glossaryIds={"keyword_102":["锁定技"],"keyword_103":["限定技"],"keyword_031":["阵法技"],"keyword_104":["主将技","副将技"]};Object.keys(glossaryIds).forEach(function(id){var item=data.keywords.find(function(keyword){return keyword.id===id;});if(!item)return;glossaryIds[id].forEach(function(label){var from=0,at;while((at=text.indexOf(label,from))!==-1){out.push({start:at,end:at+label.length,item:item,label:label});from=at+label.length;}});});return out;}
  function image(file,name,lazy){var frame=el("div","art-frame");if(!file){frame.append(el("span","placeholder-art",name));return frame;}
    var img=el("img");img.src="data/card_images/"+encodeURIComponent(file);img.alt=name+"牌面";if(lazy)img.loading="lazy";img.decoding="async";
    img.addEventListener("error",function(){frame.replaceChildren(el("span","placeholder-art",name));});frame.append(img);return frame;}
  function prose(text,context,exclude){text=String(text||"");var p=el("div","prose"),position=0;
    var candidates=model.findMentions(text,context,exclude,["skill","card"]).map(function(m){return {start:m.start,end:m.end,targets:m.targets,label:m.label,type:"reference"};})
      .concat(keywordPositions(text).map(function(m){return {start:m.start,end:m.end,item:m.item,label:m.label,type:"keyword"};}),suitPositions(text).map(function(m){return {start:m.start,end:m.end,suit:m.suit,type:"suit"};}));
    candidates.sort(function(a,b){return a.start-b.start||(b.end-b.start)-(a.end-a.start)||({reference:0,keyword:1,suit:2}[a.type]-{reference:0,keyword:1,suit:2}[b.type]);});
    candidates.forEach(function(m){if(m.start<position)return;p.append(document.createTextNode(text.slice(position,m.start)));
      if(m.type==="suit")p.append(suitIcon(m.suit));
      else if(m.type==="keyword"){var b=button(m.label,function(){},"inline-ref glossary-trigger");p.append(showKeywordBubble(b,m.item,m.label));}
      else {var targets=m.targets.filter(function(t){return t.kind==="skill"||t.kind==="card";});if(!targets.length){p.append(document.createTextNode(text.slice(m.start,m.end)));position=m.end;return;}var a=link(m.label,route(targets[0].kind,targets[0].id),"inline-ref");a.title="查看"+m.label+"说明";a.addEventListener("click",function(e){if(e.ctrlKey||e.metaKey||e.shiftKey)return;e.preventDefault();openReference(targets,context);});p.append(a);}
      position=m.end;});p.append(document.createTextNode(text.slice(position)));return p;}
  function heading(title,description,eyebrow){var head=el("header","page-head");if(eyebrow)head.append(el("div","eyebrow",eyebrow));head.append(el("h1","",title));if(description)head.append(el("p","",description));main.append(head);}
  function breadcrumb(section,title){var b=el("div","breadcrumb");add(b,link("资料首页","#/generals"),document.createTextNode(" / "),link(section[1],"#/"+section[0]),document.createTextNode(" / "+title));main.append(b);}
  function section(title,note){var s=el("section","section"),h=el("div","section-title");h.append(el("h2","",title));if(note)h.append(el("small","",note));s.append(h);return s;}
  function empty(title,description){var box=el("div","empty-state");add(box,el("h2","",title),el("p","",description),link("浏览全部武将","#/generals","button"));return box;}
  function generalTile(g,v){v=v||(model.variants.get(g.id)||[]).slice().sort(model.compareGeneralVariants)[0];var a=link("",route("general",g.id),"general-tile");
    a.append(image(v&&v.image,g.name,true));var caption=el("div","tile-caption"),top=el("div","tile-top"+(g.name.length>3||v&&model.factionParts(v.faction).length>1?" long-name":""));top.append(el("h3","name",g.name));
    if(v)top.append(factionMarks(v.faction,"faction-marks tile-faction"));
    caption.append(top);if(v&&v.title)caption.append(el("div","tile-title",v.title));
    a.append(caption);return a;}
  function entry(kind,item){var a=link("",route(kind,item.id),"entry"),title=kind==="faq"?item.question:item.name;
    if(kind==="card"&&isGame(item))return cardTile(item);
    if(kind==="card")a.append(el("div","entry-tag",[item.category_2,item.category_3].filter(Boolean).join(" · ")));
    add(a,el("h3","entry-title",title),el("p","entry-summary",kind==="skill"?(model.versions.get(item.id)||[]).map(function(v){return v.text;}).join("\n"):kind==="card"?cardText(item):kind==="faq"?item.answer:kind==="product"?[item.release_year,modes[item.mode],item.general_sets.join(" · ")].filter(Boolean).join(" · "):item.definition));return a;}
  function cardTile(c){var a=link("",route("card",c.id),"general-tile card-tile"),caption=el("div","tile-caption");
    add(a,image(c.image,c.name,true));add(caption,el("h3","name",c.name),el("div","tile-title",[c.category_2,c.category_3].filter(Boolean).join(" · ")));if(isRecast(c))caption.append(el("span","recast-label","可重铸"));a.append(caption);return a;}
  function isRecast(c){return model.isRecastCard(c);}
  function recastTag(c){if(!isRecast(c))return null;var item=data.keywords.find(function(k){return k.id==="keyword_064";});if(!item)return null;var b=button("可重铸",function(){},"recast-label");return showKeywordBubble(b,item,"重铸");}
  function cardText(c){return [c.effect].concat(((c.rules||{}).use_methods||[]).map(function(m){return [m.label,m.timing,m.target,m.effect].filter(Boolean).join("；");})).filter(Boolean).join("\n");}
  function source(item,parent){if(!item.source)return;var s=el("details","source");add(s,el("summary","","查看来源"),el("div","prose",item.source));parent.append(s);}
  function faqItem(item){var box=el("details","faq-item");add(box,el("summary","",item.question),prose(item.answer));source(item,box);return box;}
  function keywordItem(item){var box=el("details","faq-item keyword-item");box.append(el("summary","",item.name));keywordRows(item).forEach(function(row){box.append(prose(row));});return box;}
  function skillBlock(row,context,referenced){var box=el("article","skill-block"+(referenced?" referenced":"")),h=el("div","skill-heading");
    h.append(link((referenced?"* ":"")+row.skill.name,route("skill",row.skill.id),""));if(referenced)h.append(el("small","muted","见「"+row.from+"」"));
    h.firstChild.className="entry-title";box.append(h);
    box.append(row.version?prose(row.version.text,context,[row.skill.id]):el("p","muted","该版本的技能说明尚未收录。"));
    if(!row.version)box.append(link("查看已收录版本",route("skill",row.skill.id),"inline-ref"));return box;}
  function cardBody(c,parent,context){var tags=el("div","chip-list");[c.category_2,c.category_3,c.card_set].filter(Boolean).forEach(function(x){tags.append(el("span","tag",x));});if(tags.childNodes.length)parent.append(tags);var recast=recastTag(c);if(recast)parent.append(recast);
    function field(title,text){if(!text)return;var s=el("section","text-section");add(s,el("h3","field-title",title),prose(text,context,[c.id]));parent.append(s);}
    field("效果",c.effect);if(c.attack_range)field("攻击范围",c.attack_range);
    ((c.rules||{}).use_methods||[]).forEach(function(m,i){var s=el("section","text-section");s.append(el("h3","field-title",m.label||"使用方法"+(((c.rules||{}).use_methods||[]).length>1?" "+(i+1):"")));
      [["时机",m.timing],["目标",m.target],["效果",m.effect]].forEach(function(row){if(row[1]){s.append(el("div","muted",row[0]));s.append(prose(row[1],context,[c.id]));}});parent.append(s);});
    field("备注",c.note);
  }
  function versionBlocks(skill,parent,context){var chosen=context&&model.resolveVersion(skill.id,context),rows=chosen?[chosen]:(model.versions.get(skill.id)||[]);
    if(!rows.length)parent.append(el("p","muted","技能说明尚未收录。"));
    rows.forEach(function(v){var box=el("section","text-section"),product=model.index.product.get(v.product_id);
      box.append(el("div","version-label",[modes[v.mode]||v.mode,product&&product.name].filter(Boolean).join(" · ")));box.append(prose(v.text,context||v,[skill.id]));parent.append(box);});
  }
  function openReference(targets,context){
    dialogContent.replaceChildren();dialogContent.className="";if(targets.length>1){add(dialogContent,el("h2","dialog-title",targets[0].name),el("p","annotation","找到多个同名条目，请选择要查看的说明。"));
      dialogContent.firstChild.id="reference-title";var choices=el("div","dialog-choice");targets.forEach(function(t){var item=model.index[t.kind].get(t.id);choices.append(button(labels[t.kind]+" · "+item.name+" · "+t.id,function(){openReference([t],context);}));});dialogContent.append(choices);
    }else{var target=targets[0],item=model.index[target.kind].get(target.id),h=el("h2","dialog-title",item.name);h.id="reference-title";dialogContent.append(h);
      if(target.kind==="skill")versionBlocks(item,dialogContent,context);
      if(target.kind==="card"){dialogContent.className="dialog-card";if(item.image){var img=el("img","card-image");img.src="data/card_images/"+encodeURIComponent(item.image);img.alt=item.name+"牌面";dialogContent.append(img);}cardBody(item,dialogContent,context);}else dialogContent.className="";
      var foot=el("div","dialog-links");var a=link("查看完整"+labels[target.kind]+"条目 →",route(target.kind,target.id),"inline-ref");a.addEventListener("click",function(){dialog.close();});foot.append(a);dialogContent.append(foot);
    }if(!dialog.open)dialog.showModal();else document.getElementById("close-dialog").focus();
  }
  function generalDetail(id,params){var g=model.index.general.get(id);if(!g)return missing();var variants=model.variants.get(id)||[],v=variants.find(function(row){return row.id===params.get("version");})||variants[0];if(!v)return missing();
    breadcrumb(sections[0],g.name);var layout=el("div","detail-layout"),portrait=el("aside","portrait-column"),body=el("article");portrait.append(image(v.image,g.name,false));add(layout,portrait,body);main.append(layout);
    var head=el("header","general-heading");add(head,el("div","general-title",v.title||""),el("h1","",g.name));
    var line=el("div","identity-line");line.append(factionMarks(v.faction,"faction-marks detail-faction"));["体力 "+v.hp,{male:"男",female:"女",other:"其他"}[v.gender],v.general_set||modes[v.mode]].filter(Boolean).forEach(function(x){line.append(el("span","tag",x));});head.append(line);
    var meta=el("div","general-meta");meta.append(el("span","","编号 "+(v.general_number||"未录入")));if(v.illustrator)meta.append(el("span","","画师 "+v.illustrator));head.append(meta);body.append(head);
    if(variants.length>1){var links=el("div","chip-list section");variants.forEach(function(row){links.append(link([row.general_number,row.title,row.general_set].filter(Boolean).join(" · "),route("general",g.id)+"?version="+encodeURIComponent(row.id),"chip"));});body.append(links);}
    if(v.zhulianbihe.length){var pairs=section("珠联璧合"),chips=el("div","chip-list");v.zhulianbihe.forEach(function(name){var gs=data.generals.filter(function(x){return x.name===name;});if(!gs.length)chips.append(el("span","tag",name));
      gs.forEach(function(x){var gv=(model.variants.get(x.id)||[])[0];chips.append(link(name+(gs.length>1?" · "+gv.general_number:""),route("general",x.id),"chip"));});});pairs.append(chips);body.append(pairs);}
    var own=model.ownSkills(v),skills=section("武将技能",own.length+" 项");own.forEach(function(row){skills.append(skillBlock(row,v,false));});body.append(skills);
    var refs=model.references(v);if(refs.skills.length){var ref=section("关联技能");refs.skills.forEach(function(row){ref.append(skillBlock(row,v,true));});body.append(ref);}
    if(refs.cards.length){var cards=section("关联卡牌"),chips=el("div","chip-list");refs.cards.forEach(function(c){var a=link(c.name,route("card",c.id),"chip");a.addEventListener("click",function(e){if(e.ctrlKey||e.metaKey||e.shiftKey)return;e.preventDefault();openReference([{kind:"card",id:c.id}],v);});chips.append(a);});cards.append(chips);body.append(cards);}
    var faqs=model.relatedFaq([g.name].concat(own.map(function(row){return row.skill.name;})));if(faqs.length){var f=section("相关 FAQ");faqs.forEach(function(q){f.append(faqItem(q));});body.append(f);}
    document.title=g.name+" · "+v.title+" · 国战图鉴";
  }
  function detail(kind,id){var item=model.index[kind]&&model.index[kind].get(id);if(!item)return missing();var nav=kind==="skill"?sections[1]:kind==="card"?(isGame(item)?sections[2]:sections[3]):kind==="keyword"?sections[4]:kind==="product"?sections[6]:sections[5];
    breadcrumb(nav,item.name||"裁定");heading(item.name||item.question,"",labels[kind]);document.title=(item.name||item.question)+" · 国战图鉴";
    if(kind==="skill"){versionBlocks(item,main);var owners=[];data.general_variants.forEach(function(v){if(v.skills.some(function(r){return r.skill_id===id;}))owners.push(v);});owners.sort(model.compareGeneralVariants);if(owners.length){var s=section("拥有此技能的武将"),grid=el("div","general-grid");owners.forEach(function(v){grid.append(generalTile(model.index.general.get(v.general_id),v));});s.append(grid);main.append(s);}
      var qs=model.relatedFaq([item.name]);if(qs.length){var f=section("相关 FAQ");qs.forEach(function(q){f.append(faqItem(q));});main.append(f);}}
    if(kind==="card"){var wrap=el("div","card-detail"),body=el("article");if(item.image){var img=el("img","card-image");img.src="data/card_images/"+encodeURIComponent(item.image);img.alt=item.name+"牌面";wrap.append(img);}cardBody(item,body);wrap.append(body);main.append(wrap);}
    if(kind==="keyword")keywordRows(item).forEach(function(row){main.append(prose(row));});if(kind==="faq"){main.append(prose(item.answer));source(item,main);}
    if(kind==="product"){main.append(el("p","muted",[item.release_year&&"发行年份 "+item.release_year,modes[item.mode]].filter(Boolean).join(" · ")));
      var sets=section("武将牌组"),chips=el("div","chip-list");item.general_sets.forEach(function(name){chips.append(el("span","tag",name));});sets.append(chips);main.append(sets);
      var cards=section("游戏牌拓展包"),cardSets=el("div","chip-list");item.card_sets.forEach(function(name){cardSets.append(el("span","tag",name));});cards.append(cardSets);main.append(cards);}
  }
  function select(name,label,values,current,keepOrder){var s=el("select");s.name=name;s.setAttribute("aria-label",label);s.append(new Option("全部"+label,""));var options=values.filter(Boolean).filter(function(x,i,a){return a.indexOf(x)===i;});if(!keepOrder)options.sort(function(a,b){return a.localeCompare(b,"zh-CN-u-co-pinyin");});options.forEach(function(x){s.append(new Option(x,x));});s.value=current||"";return s;}
  function listing(key,params){var config=sections.find(function(x){return x[0]===key;});if(!config)return missing();heading(config[1],"","群狼环鼎 / 资料索引");
    var kind={generals:"general",skills:"skill",cards:"card",markers:"card",keywords:"keyword",faq:"faq",products:"product"}[key],rows=kind==="general"?data.generals:kind==="skill"?data.skills:kind==="card"?data.cards.filter(function(c){return key==="cards"?isGame(c):!isGame(c);}):kind==="keyword"?data.keywords:kind==="product"?data.products:data.faq;
    if(kind==="general"||key==="cards"){
      var form=el(kind==="general"?"div":"form","toolbar");
      if(kind==="general"){var current=params.get("faction")||"",group=el("div","faction-filter");[["吴","吴"],["蜀","蜀"],["魏","魏"],["群","群"],["野","野心家"],["晋","晋"]].forEach(function(row){var b=button(row[0],function(){var next=current===row[1]?"":row[1];location.hash="#/generals"+(next?"?faction="+encodeURIComponent(next):"");},"faction-filter-button"+(current===row[1]?" selected":""));b.setAttribute("aria-pressed",String(current===row[1]));group.append(b);});form.append(group);}
      if(key==="cards"){
        var category=select("category","分类",model.cardCategories,params.get("category"),true),types=select("type","类型",[],params.get("type"),true);
        function refreshTypes(){var current=types.value||params.get("type")||"",values=category.value?(model.cardTypes[category.value]||[]):model.cardCategories.flatMap(function(c){return model.cardTypes[c];});
          var next=select("type","类型",values,current,true);types.replaceChildren.apply(types,Array.from(next.childNodes));types.value=values.includes(current)?current:"";}
        refreshTypes();category.addEventListener("change",refreshTypes);add(form,category,types);
      }
      if(key==="cards"){var submit=el("button","button primary","筛选");submit.type="submit";form.append(submit);if(params.toString())form.append(link("清除","#/"+key,"button"));form.addEventListener("submit",function(e){e.preventDefault();var p=new URLSearchParams();new FormData(form).forEach(function(value,name){if(value)p.set(name,value);});location.hash="#/"+key+(p.toString()?"?"+p.toString():"");});}else if(current)form.append(link("清除筛选","#/generals","button"));main.append(form);
    }
    var filtered=rows.filter(function(item){
      if(kind==="general")return (model.variants.get(item.id)||[]).some(function(v){return model.matchesFaction(v.faction,params.get("faction"));});
      return key!=="cards"||((!params.get("category")||item.category_2===params.get("category"))&&(!params.get("type")||item.category_3===params.get("type")));});
    if(kind==="general")filtered.sort(model.compareGenerals);
    else if(key==="cards")filtered.sort(model.compareCards);
    else if(["skills","markers","keywords","faq"].includes(key))filtered.sort(model.compareEntries);
    main.append(el("p","result-note","共 "+filtered.length+" 条"+(filtered.length!==rows.length?" / 全部 "+rows.length+" 条":"")));
    if(!filtered.length){main.append(empty("没有找到匹配的条目","可以缩短搜索词，或清除当前筛选条件。"));return;}
    var grid=el("div",kind==="general"||key==="cards"?"general-grid":kind==="faq"||kind==="keyword"?"faq-list":"entry-grid");filtered.forEach(function(item){var variant=kind==="general"?(model.variants.get(item.id)||[]).find(function(v){return model.matchesFaction(v.faction,params.get("faction"));}):null;grid.append(kind==="general"?generalTile(item,variant):kind==="faq"?faqItem(item):kind==="keyword"?keywordItem(item):entry(kind,item));});main.append(grid);
  }
  function search(params){var q=params.get("q")||"";heading("检索资料",q?"搜索「"+q+"」":"输入姓名、技能、卡名或规则关键词。","全库检索");var total=0;
    if(q.trim())["general","skill","card","keyword","faq","product"].forEach(function(kind){var rows=Array.from(model.index[kind].values()).filter(function(item){return model.matchesQuery(model.searchable(kind,item),q);});if(!rows.length)return;total+=rows.length;
      if(kind==="card")rows.sort(function(a,b){return isGame(a)&&isGame(b)?model.compareCards(a,b):isGame(a)?-1:isGame(b)?1:model.compareEntries(a,b);});else if(kind==="general")rows.sort(model.compareGenerals);else rows.sort(model.compareEntries);
      var s=section(labels[kind],rows.length+" 条"),grid=el("div",kind==="general"||kind==="card"?"general-grid":kind==="faq"||kind==="keyword"?"faq-list":"entry-grid");rows.forEach(function(item){grid.append(kind==="general"?generalTile(item):kind==="faq"?faqItem(item):kind==="keyword"?keywordItem(item):entry(kind,item));});s.append(grid);main.append(s);});
    if(!total)main.append(empty(q?"没有找到相关资料":"开始查阅","使用顶部搜索框检索全部资料。"));document.getElementById("search-input").value=q;
  }
  function missing(){heading("条目未找到","该条目可能已在复核中删除。");main.append(empty("返回资料索引","从索引或搜索查找当前保留的资料。"));}
  function render(){if(location.hash==="#main-content"){main.focus();return;}if(dialog.open)dialog.close();main.replaceChildren();document.title="国战图鉴 · 群狼环鼎";
    var raw=location.hash.slice(1)||"/generals",parts=raw.split("?"),path=parts[0].split("/").filter(Boolean),params=new URLSearchParams(parts.slice(1).join("?")),key=path[0]||"generals",id;try{id=decodeURIComponent(path[1]||"");}catch(e){id="";}
    var active=key==="general"?"generals":key==="skill"?"skills":key==="card"?(model.index.card.get(id)&&!isGame(model.index.card.get(id))?"markers":"cards"):key==="keyword"?"keywords":key==="product"?"products":key;
    document.getElementById("navigation").replaceChildren();sections.forEach(function(s){var a=link("","#/"+s[0],"nav-item");add(a,el("span","",s[1]),el("span","nav-count",s[2]));if(active===s[0])a.setAttribute("aria-current","page");document.getElementById("navigation").append(a);});
    if(key==="general"&&id)generalDetail(id,params);else if(["skill","card","keyword","faq","product"].includes(key)&&id)detail(key,id);else if(key==="search")search(params);else listing(key,params);
    window.scrollTo(0,0);main.focus({preventScroll:true});
  }
  document.getElementById("close-dialog").addEventListener("click",function(){dialog.close();});
  dialog.addEventListener("click",function(e){if(e.target===dialog){var r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});
  document.addEventListener("click",function(e){if(popover&&!popover.host.contains(e.target)){popover.bubble.hidden=true;popover.trigger.setAttribute("aria-expanded","false");popover=null;}});
  document.getElementById("global-search").addEventListener("submit",function(e){e.preventDefault();var q=document.getElementById("search-input").value.trim();location.hash="#/search?q="+encodeURIComponent(q);});
  document.addEventListener("keydown",function(e){if(e.key==="Escape"&&popover){popover.bubble.hidden=true;popover.trigger.setAttribute("aria-expanded","false");popover=null;}if(e.key==="/"&&!dialog.open&&!["INPUT","TEXTAREA","SELECT"].includes(document.activeElement.tagName)){e.preventDefault();document.getElementById("search-input").focus();}});
  window.addEventListener("hashchange",render);render();
})();
