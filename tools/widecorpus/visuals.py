"""Prepare matched cameras for ranked regressions, submit to the local broker and assemble returned sheets."""
import argparse
import io
import math
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError

from PIL import Image, ImageDraw, ImageOps

from common import HERE, REVAMPED, ROOT, TREE, digest, places, read, save

sys.path.insert(0, str(HERE.parent))
import streetview
import numpy as np


# Older brokers could inherit launcher experiments during a nominal default capture.
BROKER_PROTOCOL = "unrequested-experiments-off-v1"


def photos(place, dst):
    cached = dst / "photos.json"
    if cached.is_file():
        return read(cached)["photos"][:3]
    found = {}
    token = streetview.token()
    options = []
    for radius in (60,125,250,400):
        try:
            for item in streetview.search(place["lat"], place["lon"], radius, token, limit=160):
                found[item["id"]] = item
        except HTTPError as exc:
            if exc.code in (401,403,429):
                raise
            if not found and radius==400:
                raise
            continue
        except (OSError,ValueError):
            if not found and radius==400:
                raise
            continue
        images = list(found.values())
        options = streetview.pick(images, place["lat"], place["lon"], 30)
        # A spherical photo also supplies a view looking back toward the actual junction.
        for item in images:
            if not item.get("is_pano") or not item.get("thumb_2048_url"):
                continue
            xy=(item.get("computed_geometry") or item.get("geometry") or {}).get("coordinates")
            heading=item.get("computed_compass_angle",item.get("compass_angle"))
            if xy and heading is not None:
                lon,lat=xy
                dx=(place["lon"]-lon)*math.cos(math.radians(lat))
                dy=place["lat"]-lat
                options.append((item,lat,lon,math.degrees(math.atan2(dx,dy))%360))
        if len(options)>=3:
            break
    if len(options)<3:
        for item in found.values():
            if item.get("camera_type")!="fisheye" or item.get("is_pano") or not item.get("thumb_2048_url"):
                continue
            xy=(item.get("computed_geometry") or item.get("geometry") or {}).get("coordinates")
            heading=item.get("computed_compass_angle",item.get("compass_angle"))
            if xy and heading is not None:
                options.append((item,xy[1],xy[0],heading))
    selected = []
    while options and len(selected)<3:
        def priority(o):
            item, lat, lon, heading = o
            dx=(place["lon"]-lon)*math.cos(math.radians(lat))*111320
            dy=(place["lat"]-lat)*111320
            toward=(math.degrees(math.atan2(dx,dy))-heading+180)%360-180
            turn=min((abs((heading-s["heading"]+180)%360-180) for s in selected),default=180)
            artist=(item.get("creator") or {}).get("username","unknown")
            return (abs(toward)>75, turn<40, any(s["creator"]==artist for s in selected),
                    abs(math.hypot(dx,dy)-25), str(item["id"]))
        item,lat,lon,angle=best=min(options,key=priority)
        options.remove(best)
        name=f'mly_{int(item["id"])}.jpg'
        try:
            img=ImageOps.exif_transpose(Image.open(io.BytesIO(streetview.get(item["thumb_2048_url"])))).convert("RGB")
        except (OSError,ValueError):
            continue
        if item.get("is_pano"):
            rgb=np.asarray(img)
            h,w=rgb.shape[:2]
            if w<1.8*h:
                continue
            native_heading=item.get("computed_compass_angle",item.get("compass_angle"))
            x,y=np.meshgrid((np.arange(800)-399.5)/400,(224.5-np.arange(450))/400)
            yaw=np.arctan(x)+math.radians(angle-native_heading)
            pitch=np.arctan(y/np.sqrt(1+x*x))
            ix=((.5+yaw/(2*math.pi))*w).astype(int)%w
            iy=np.clip(((.5-pitch/math.pi)*h).astype(int),0,h-1)
            img=Image.fromarray(rgb[iy,ix])
        img.save(dst/name,quality=92)
        selected.append(dict(file=name,id=str(item["id"]),lat=lat,lon=lon,heading=angle,
                             projection="panorama crop" if item.get("is_pano") else item.get("camera_type"),
                             creator=(item.get("creator") or {}).get("username","unknown")))
    if selected:
        save(cached,{"photos":selected,"credit":"Mapillary contributors, CC BY-SA 4.0"})
    return selected


def batch_shots(entries, first):
    return [{**shot, "name": f'p{i}_{shot["name"]}'}
            for i, entry in enumerate(entries[first:first+3], start=first)
            for shot in entry["shots"]]


def references(run, place):
    dst=run/"references"/place["id"]
    dst.mkdir(parents=True,exist_ok=True)
    if not (dst/"aerial.jpg").is_file():
        proc=subprocess.run([sys.executable,HERE.parent/"ortho_hi.py",str(place["lat"]),str(place["lon"]),
                             "130",dst/"aerial.jpg","auto","1280"],capture_output=True,text=True,
                            env={**os.environ,"HOME":str(dst),"PYTHONDONTWRITEBYTECODE":"1"})
        (dst/"aerial-source.txt").write_text(proc.stdout+proc.stderr)
    try:
        return photos(place,dst)
    except (OSError,ValueError,SystemExit) as exc:
        (dst/"photo-error.txt").write_text(type(exc).__name__)
        return []


def prepare(run, selected, configs, folder, population=None, overhead_only=False):
    folder.mkdir(parents=True, exist_ok=True)
    entries=[]
    for pid in selected:
        place=places(pid)[0]
        refs=references(run,place)
        camera=[-35,-45,55,35,45,-55]
        if overhead_only:
            # In a street canyon an offset camera can end up looking at a roof.
            camera=[0,0,145,0,.01,-1]
            refs=[]
        group=[dict(name="overview",lat=place["lat"],lon=place["lon"],cam=camera)]
        for i,photo in enumerate(refs):
            angle=math.radians(photo["heading"])
            height=float(photo.get("ground_offset_m",2.0))
            pitch=math.radians(float(photo.get("pitch_deg",0)))
            if not math.isfinite(height) or not -90<=math.degrees(pitch)<=90:
                raise ValueError("invalid reference camera")
            group.append(dict(name=f"street{i+1}",lat=photo["lat"],lon=photo["lon"],
                              cam=[0,0,height,round(math.cos(pitch)*math.sin(angle),6),
                                   round(math.cos(pitch)*math.cos(angle),6),round(math.sin(pitch),6)]))
        entries.append(dict(id=pid,refs=refs,shots=group))
    identity=digest([BROKER_PROTOCOL,"pc",str(TREE),read(run/"manifest.json")["code"],entries,configs])[:12]
    reqs={}
    for i in range(0,len(entries),3):
        shots=batch_shots(entries,i)
        for label,env in configs.items():
            name=f"wide-{identity}-{i//3}-{label.lower()}"
            save(folder/(name+".json"),dict(tree=str(TREE),device="pc",env=env,shots=shots))
            reqs[f"{i//3}/{label}"]=name
    previous=read(folder/"index.json") if (folder/"index.json").is_file() else {}
    for key,base in reqs.items():
        prior=previous.get("requests",{}).get(key,"")
        prior_file=folder/(prior+".json")
        if prior.startswith(base+"-retry") and prior_file.is_file() and read(prior_file)==read(folder/(base+".json")):
            reqs[key]=prior
    population=list(population) if population is not None else None
    if previous.get("requests")!=reqs or previous.get("population")!=population:
        (folder/"sheets.md").unlink(missing_ok=True)
    save(folder/"index.json",dict(entries=entries,requests=reqs,configs=list(configs),protocol=BROKER_PROTOCOL,
                                  code=read(run/"manifest.json")["code"],population=population,overhead_only=overhead_only))


def receipt_images(broker, name, cameras):
    receipt=read(broker/(name+".done"))
    if receipt.get("device","pc")!="pc":
        raise ValueError(f"{name} was captured on a different device")
    files=[Path(p) for p in receipt["files"]]
    if len(files)!=len(cameras) or any(p.resolve().parent!=(broker/name).resolve() for p in files):
        raise ValueError(f"{name} receipt points outside its capture set or has the wrong size")
    # The local single-device broker uses shot.jpg; the newer broker uses pc-shot.jpg.
    prefix="pc-" if "device" in receipt else ""
    got={p.stem.removeprefix(prefix):p for p in files if p.stem.startswith(prefix)}
    expected={s["name"] for s in cameras}
    if got.keys()!=expected or not all(p.is_file() for p in got.values()):
        raise ValueError(f"{name} incomplete image set")
    for path in got.values():
        with Image.open(path) as img:
            img.verify()
    return got


def submit(folder, broker, retry_failed=False):
    broker.mkdir(parents=True,exist_ok=True)
    index=read(folder/"index.json")
    if index.get("protocol")!=BROKER_PROTOCOL:
        raise ValueError("replan captures with the current broker")
    for key,name in list(index["requests"].items()):
        path=broker/(name+".json")
        request=read(folder/(name+".json"))
        if request.get("device")!="pc":
            raise ValueError("replan captures for the local PC")
        if retry_failed and path.exists():
            if read(path)!=request:
                raise ValueError(f"broker name collision {name}")
            failed=(broker/(name+".err")).is_file()
            if (broker/(name+".done")).is_file():
                try:
                    receipt_images(broker,name,request["shots"])
                except (ValueError,KeyError,OSError):
                    failed=True
            if failed:
                # Preserve the failed receipt. A crash after saving the plan can resume normal submission.
                name=name+"-retry"
                path=broker/(name+".json")
                save(folder/(name+".json"),request)
                index["requests"][key]=name
                save(folder/"index.json",index)
                (folder/"sheets.md").unlink(missing_ok=True)
        if path.exists():
            if read(path)!=request:
                raise ValueError(f"broker name collision {name}")
        else:
            save(path,request)


def sheets(run, folder, broker):
    index=read(folder/"index.json")
    if index.get("protocol")!=BROKER_PROTOCOL:
        raise ValueError("fresh captures required with the current broker")
    manifest=read(run/"manifest.json")
    if index.get("code")!=manifest["code"]:
        raise ValueError("capture plan and corpus build differ")
    expected_envs=manifest["configs"].copy()
    for label in index["configs"]:
        if label.startswith("without-") and expected_envs.get("revamped")==REVAMPED:
            omitted="BNE_"+label.removeprefix("without-").upper()
            if omitted in REVAMPED:
                expected_envs[label]={k:v for k,v in REVAMPED.items() if k!=omitted}
    if set(expected_envs)!=set(index["configs"]) or expected_envs.get("default")!={}:
        raise ValueError("capture configurations differ from the corpus run")
    done={}
    for key,name in index["requests"].items():
        end=broker/(name+".done")
        if (broker/(name+".err")).exists():
            raise RuntimeError(f"broker error for {name}")
        if not end.exists():
            raise RuntimeError(f"awaiting {name}")
        request=read(folder/(name+".json"))
        if request!=read(broker/(name+".json")):
            raise ValueError(f"{name} broker request differs from the saved plan")
        batch,label=key.split("/")
        cameras=batch_shots(index["entries"],3*int(batch))
        if request["shots"]!=cameras:
            raise ValueError(f"{name} cameras differ from plan")
        if request["env"]!=expected_envs[label]:
            raise ValueError(f"{name} switch environment differs from the corpus comparison")
        done[key]=receipt_images(broker,name,cameras)
    lines=["# Matched camera sheets",""]
    if index.get("population"):
        n,m=index["population"]
        lines.extend([f"Highest regressions among {n} ranked places in a selection of {m}. Confirmed production crashes rank first. Geometry scores require complete tile pairs.",""])
    for i,e in enumerate(index["entries"]):
        pid=e["id"]
        refs=run/"references"/pid
        rows=e["shots"]
        overhead=index.get("overhead_only",False)
        labels=(["Aerial"] if overhead else ["Aerial","Mapillary"])+index["configs"]
        w,h=450,278
        sheet=Image.new("RGB",(w*len(labels),h*len(rows)+30),(24,24,24))
        pen=ImageDraw.Draw(sheet)
        for j,label in enumerate(labels):
            pen.text((j*w+8,7),label,fill="white")
        for j,shot in enumerate(rows):
            files=[refs/"aerial.jpg"]
            if not overhead:
                files.append(refs/e["refs"][j-1]["file"] if j else None)
            for label in index["configs"]:
                files.append(done[f"{i//3}/{label}"][f'p{i}_{shot["name"]}'])
            for col,path in enumerate(files):
                y=30+j*h
                if path and path.is_file():
                    with Image.open(path) as img:
                        image=ImageOps.contain(img.convert("RGB"),(w-4,h-25))
                    sheet.paste(image,(col*w,y))
                elif path:
                    pen.text((col*w+8,y+40),"Reference unavailable",fill="white")
                if col==0:
                    caption="Overview" if j==0 else f'Street {j}   heading {e["refs"][j-1]["heading"]:.0f} deg'
                    pen.text((6,y+h-22),caption,fill="white")
                if j and col==1:
                    ref=e["refs"][j-1]
                    pen.text((w+6,y+h-22),f'{ref["id"]} / {ref["creator"]} / {ref.get("projection") or "photo"}',fill="white")
        out=folder/(pid+".jpg")
        sheet.save(out,quality=91)
        lines+=[f"## {pid}","",f"![Aerial, photo and game cameras]({out})",""]
        receipts=[f'[{label} broker receipt]({broker/(index["requests"][f"{i//3}/{label}"]+".done")})'
                  for label in index["configs"]]
        lines.extend([" · ".join(receipts),""])
        if (refs/"aerial-source.txt").is_file():
            lines += [f'[Aerial source record]({refs/"aerial-source.txt"})',""]
        if e["refs"]:
            lines += [f'Street photography courtesy of the credited Mapillary contributors, CC BY-SA 4.0. '
                      f'[Photo IDs, authors and camera headings]({refs/"photos.json"})',""]
        if not (refs/"aerial.jpg").is_file():
            lines.append("Aerial unavailable.\n")
        if len(e["refs"])<3 and not index.get("overhead_only"):
            lines.append(f'{len(e["refs"])} street photos available.\n')
    (folder/"sheets.md").write_text("\n".join(lines))
    if (run/"comparison.json").is_file():
        from compare import write_comparison
        write_comparison(run,read(run/"comparison.json"))


def main():
    os.umask(0o077)
    ap=argparse.ArgumentParser()
    ap.add_argument("action",choices=["plan","submit","sheets"])
    ap.add_argument("--run", default="current")
    ap.add_argument("--set",default="worst")
    ap.add_argument("--places")
    ap.add_argument("--without",choices=sorted(REVAMPED),help="also capture Revamped with this switch omitted")
    ap.add_argument("--worst",type=int,default=3)
    ap.add_argument("--allow-incomplete",action="store_true",help="label the ranking as partial if places are missing")
    ap.add_argument("--overhead-only",action="store_true",help="plan one centred overhead camera per place for an obstructed overview")
    ap.add_argument("--retry-failed",action="store_true",help="resubmit terminal failures or incomplete receipts with the same cameras")
    ap.add_argument("--broker",type=Path,default=Path.home()/"bnge-tests/codexwide/gamereq")
    a=ap.parse_args()
    if a.retry_failed and a.action!="submit":
        ap.error("--retry-failed applies to submit")
    if a.worst<1 or any(Path(x).name!=x or x in ("",".","..") for x in (a.run,a.set)):
        ap.error("invalid selection")
    run=ROOT/"runs"/a.run
    folder=run/"visuals"/a.set
    if a.action=="plan":
        manifest=read(run/"manifest.json")
        population=None
        if a.places:
            selected=[p["id"] for p in places(a.places)]
        else:
            from compare import ranking, worst_places
            result=ranking(run,places(",".join(manifest["places"])),manifest["configs"],manifest["code"],manifest["radius"])
            examined=result["complete"]+len(result["build_failures"])
            if not examined or (result["missing"] and not a.allow_incomplete):
                ap.error("complete ranking required for --worst")
            population=(examined,result["expected"])
            selected=worst_places(result,a.worst)
        configs=manifest["configs"].copy()
        if a.without:
            if configs.get("revamped")!=REVAMPED:
                ap.error("--without requires the full Revamped run")
            configs["without-"+a.without[4:].lower()]={k:v for k,v in REVAMPED.items() if k!=a.without}
        prepare(run,selected,configs,folder,population,a.overhead_only)
    elif a.action=="submit":
        from run import fingerprint
        plan=read(folder/"index.json")
        if plan.get("code") and plan["code"]!=fingerprint():
            ap.error("shot plan and current checkout differ")
        submit(folder,a.broker,a.retry_failed)
    else:
        sheets(run,folder,a.broker)


if __name__=="__main__":
    main()
