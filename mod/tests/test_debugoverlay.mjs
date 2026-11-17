import assert from "node:assert/strict"
import { paintDebugFeatures } from "../beamng_earth/ui/ui-vue/mods/beamngEarth/debugoverlay.js"
const calls=[]
const ctx={beginPath(){calls.push(["begin"])},moveTo(x,y){calls.push(["move",x,y])},lineTo(x,y){calls.push(["line",x,y])},
  closePath(){},fill(rule){calls.push(["fill",rule])},stroke(){calls.push(["stroke"])}}
const opts={screen:(lat,lon)=>({x:lon,y:lat}),width:100,world:360,alpha:.28}
const outer=[[10,10],[90,10],[90,90],[10,90]],hole=[[30,30],[30,70],[70,70],[70,30]]
paintDebugFeatures(ctx,{features:[{type:2,label:"sampled",rings:[outer,hole],unwrapped:true}]},opts)
assert.deepEqual(calls.filter(c=>c[0]==="fill"),[["fill","evenodd"]],"one fill preserves the hole without dark overlaps")
assert.equal(calls.filter(c=>c[0]==="stroke").length,0,"labels never create grid outlines")
assert.equal(ctx.globalAlpha,1)
calls.length=0
paintDebugFeatures(ctx,{features:[{type:3,ring:outer,outline:true}]},opts)
assert.equal(calls.filter(c=>c[0]==="stroke").length,1,"explicit regional boundary retains outline")
calls.length=0
paintDebugFeatures(ctx,{features:[{rings:[[[-180,0],[180,0],[180,10],[-180,10]]],unwrapped:true}]},opts)
assert.ok(calls.some(c=>c[0]==="line"&&c[1]===180),"full-world contours do not collapse to a meridian")
calls.length=0
paintDebugFeatures(ctx,{features:[{rings:[[[179,0],[181,0],[181,1],[179,1]]],unwrapped:true}]},
 {...opts,screen:(lat,lon)=>({x:lon>180?lon-360:lon,y:lat}),width:200})
assert.ok(calls.some(c=>c[0]==="line"&&c[1]===181),"date-line contours stay continuous")
calls.length=0
paintDebugFeatures(ctx,{features:[]},opts)
assert.equal(calls.length,0,"empty response draws nothing")
console.log("debug overlay renderer passed")
