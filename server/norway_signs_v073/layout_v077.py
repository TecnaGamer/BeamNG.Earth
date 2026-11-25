"""Data-driven sign content layout; no destination-specific substitutions."""
import re

def content_rows(lines,kind):
    result=[];pending=[]
    for line in lines:
        badges=[p for p in line if p[0] in ('route','symbol')]
        texts=[p[1] for p in line if p[0]=='text' and p[1].strip()]
        if not texts:
            pending.extend(badges);continue
        badges=pending+badges;pending=[]
        if kind=='725':
            text=' '.join(texts)
            # Handle both "Bud 35 Eide 26" and delimiter-separated records.
            pairs=list(re.finditer(r'([^\d]+?)\s+(\d+(?:[.,]\d+)?)\b',text))
            if pairs and ''.join(m.group(0) for m in pairs).replace(' ','')==text.replace(' ',''):
                for i,m in enumerate(pairs):result.append((m.group(1).strip(),m.group(2),badges if i==0 else []))
            else:result.append((text,'',badges))
        else:
            for i,t in enumerate(texts):result.append((t,'',badges if i==0 else []))
    if pending:result.append(('','',pending))
    return result

def wrap(draw,text,font,width):
    words=text.split();lines=[];line=''
    for word in words:
        candidate=(line+' '+word).strip()
        b=draw.textbbox((0,0),candidate,font=font)
        if line and b[2]-b[0]>width:lines.append(line);line=word
        else:line=candidate
    if line:lines.append(line)
    return lines or ['']
