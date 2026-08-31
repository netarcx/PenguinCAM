# PenguinCAM Quick Reference

**Student & Mentor Cheat Sheet - FRC Team 6238**

---

## 🚀 Quick Start (3 Steps)

### 1. Send Part from Onshape ⭐

**Easiest Method - One Click:**
1. Open your Part Studio in Onshape
2. **Right-click the part** in the feature tree (left sidebar)
3. Click **"Send to PenguinCAM"** from the menu
4. Part opens automatically in PenguinCAM!

**First Time Only:** You'll sign in with your @popcornpenguins.com Google account

**Alternative:** Manual DXF upload (see below)

---

### 2. Set Parameters (Setup Mode)

* Select the correct material type (polycarb, plywood, or aluminum, or aluminum tube)
* Use calipers to verify the material thickness and set the measured material thickness in PenguinCAM.  PenguinCAM will automatically determine feed height, retract height, and clearance height based on the material thickness and the material type. 
* Ensure the tool diameter is set correctly (0.157" for a 4mm end mill)

---

### 3. Orient Your Part (Setup Mode)

After import, you'll see a **2D top-down view** of your part:

1. **Check orientation** - Is it rotated how you want?
2. Click **"Rotate 90° CW"** to match your stock material
3. **Origin is always bottom-left** (X→ Y↑)
   - Like 3D printer slicers or laser cutters
   - No need to pick a corner!

**Important:** First decide how you will fixture the raw material on the machine, and then select orientation in PenguinCAM to match.

---

### 4. Generate & Download

1. Click **"Generate G-code"** 
2. **Review 3D preview** - Rotate to check toolpaths
3. **Use scrubber** to step through each cut
4. Click **"Download G-code"** OR **"Save to Google Drive"**

Done! 🎉

---

## 🔧 CNC Machine Setup

### Leveling the bed / surfacing the spoilboard

Open **Level bed** in PenguinCAM when the spoilboard needs to be made parallel to the
machine's XY travel. Enter the surfacing cutter, area, shallow cut depth, stepover, feed,
and spindle speed; inspect the raster preview before downloading the `.nc` file.

- Set G54 X0/Y0 at the lower-left of the area.
- Set G54 Z0 on the current spoilboard top. The program cuts downward from that surface.
- Dry-run the full XY travel above the board first and verify clamps are outside the area.
- The program pauses before starting the spindle so the zero and cutter can be checked.

### Before You Start

**Material:**
- Clamp material flat to sacrifice board (or to tubing jig for tubing)
- No gaps between material and sacrifice board
- Material must be fully supported and clamped

**Tools Needed:**
- End mill (must match the end mill selected in CAM)

---

### X & Y Zeroing

**For plates: Set X & Y zero at LOWER-LEFT corner of your part:**

1. **Jog** tool to lower-left corner of part
2. Click the the "Zero X" and "Zero Y" buttons

---

### Z Zeroing

⚠️ **CRITICAL: by default Z=0 is at the SACRIFICE BOARD (bottom), NOT the material
top. Read the program header before you touch off** - it names the surface that job was
generated for:

```gcode
(Z-AXIS REFERENCE:)
(  Z=0 is at SACRIFICE BOARD surface)      <- or TOP OF STOCK
```

**Setup (default, sacrifice board):**
1. Jog end mill to location outside the raw material
2. Use touch plate to set Z on the top of the sacrifice board
3. Verify Z shows as zero in Mach 4.  This should not need to be changed, but nonetheless should be verified before cutting parts.

**Setup (jobs generated with `Zero Z on: Stock top`):**
1. Jog the end mill over the material
2. Touch off on the **top face of the stock** and set Z=0 there
3. Every cutting move in that program is negative; the deepest is the thickness plus
   the overcut

**Why sacrifice board (the default)?**
- ✅ Guaranteed cut-through (0.02" overcut built in)
- ✅ Same Z zero point for all jobs
- ✅ No math when material thickness changes

See [Z_COORDINATE_SYSTEM.md](Z_COORDINATE_SYSTEM.md) for detailed explanation.

---

## 📐 Default Settings

**Material:**
- Thickness: **0.25"** (1/4" aluminum)
- Change in PenguinCAM if using different thickness

**Tool:**
- Diameter: **0.157"** (4mm endmill)
- Also works: 1/8" (0.125"), 1/4" (0.250")

**Tabs:**
- Count: **4** (evenly spaced)
- Width: **0.25"**
- Height: **0.03"** (material left uncut)
- You'll break these off after machining

---

## 🕳️ What PenguinCAM Knows

### Holes (Automatic Detection)

**All Circular Holes:**
- Helical entry + spiral clearing strategy
- Preserves exact CAD dimensions - what you draw is what you get
- Holes too small for your tool (< 1.2× tool diameter) are skipped
- Works for all sizes: #10 screws (0.201"), bearings (1.125"), or custom

### Pockets

**Inner closed shapes:**
- Full-depth plunge and cut
- Automatically offset for tool width

### Perimeter

**Outer boundary:**
- Cut with holding tabs
- Offset for correct final size
- Tabs only on straight sections (not curves!)

---

## ⚙️ Common Settings to Adjust

In PenguinCAM web interface:

**Material Thickness:**
- Measure with calipers!
- Don't guess - accuracy matters
- Common: .0875 (1/16"), 0.125" (1/8"), 0.25" (1/4"), 0.5" (1/2")

**Tool Diameter:**
- Check your actual endmill
- Common: 4mm (0.157"), 1/8" (0.125"), 1/4" (0.250")
- Wrong diameter = wrong part size!
- Feeds and depth-per-pass automatically scale DOWN for tools smaller than 4mm
  (the presets are tuned for a 4mm bit; running a 1/8" bit at 4mm numbers snaps it).
  The G-code header says when a program derated itself. Larger tools keep the
  tested preset numbers.

**Number of Tabs:**
- Small parts: 3-4 tabs
- Large parts: 6-8 tabs
- More tabs = more secure but more cleanup

**Max Depth Per Pass (optional):**
- Blank = automatic (tested preset, scaled to your tool)
- Enter a smaller depth (e.g. 0.05") to split profiles and pockets into more,
  shallower passes - the way to baby fragile or multi-flute cutters
- It only ever lowers the automatic value; the program header notes the limit
- Works in single-tool and multi-tool jobs (`max_pass_depth` in a job file,
  `--max-pass-depth` on the CLI)

**Deburring / Chamfer Pass (2D mode, optional):**
- Tick the box on the Setup step, then set your V-bit's diameter and included angle
  and how wide an edge break you want (0.02" is a light deburr)
- Pick which edges to break: outside profile, holes, pockets
- The program cuts everything with your end mill first, then **pauses (M0)** for you to
  swap in the V-bit and re-zero G54 Z on the sacrifice board - do NOT touch X/Y zero
- Tabs stay in until after the chamfer; if tab removal is enabled the program pauses
  again to swap the end mill back
- Depth is computed from the width and bit angle (a 90° bit cuts as deep as the break
  is wide; a 60° bit goes deeper); jobs that can't work are refused with an explanation

---

## 🎯 Design Tips for Onshape

### For Best Results:

**✅ Do:**
- Design parts as flat plates (or rectangular tubes)
- Use standard hole sizes when possible:
  - 0.201" for free fit #10 screws
  - 1.125" for bearings
- Make perimeter a closed loop (no gaps!)
- Put pockets fully inside the perimeter

**❌ Avoid:**
- 3D features (PenguinCAM only processes top face)
- Open paths (must be closed shapes)
- Overlapping geometry
- Tiny features smaller than tool diameter


---

## 💾 Saving Your Work

### Download G-code (Always Works)

1. Click **"Download G-code"**
2. File saves to your computer
3. Load onto CNC via USB/network

### Save to Google Drive (Recommended)

1. Click **"Save to Google Drive"**
2. Uploads to: **Shared drives → Popcorn Penguins → CNC → G-code**
3. Everyone on team can access
4. Files stay organized
5. Survives when students graduate

---

## 🔍 Checking Your G-code

### In PenguinCAM (Before Download):

**3D Preview:**
- Rotate view with mouse drag
- Zoom with scroll wheel
- Look for:
  - ✅ All holes are cut
  - ✅ Pockets are milled
  - ✅ Perimeter has tabs
  - ✅ Nothing looks wrong

**Settings Summary:**
- Check material thickness is correct
- Verify tool diameter matches your endmill
- Note number of tabs

### On CNC (Before Running):

**Dry Run:**
1. Load G-code
2. **Turn spindle OFF**
3. Run program in "single block" or slow mode
4. Watch tool path - does it look right?
5. Check clearances - will tool hit clamps?

**Then:**
1. Return to start
2. Turn spindle ON
3. Run program for real!

---

## 🚨 Safety Checklist

**Before Running G-code:**

- [ ] Correct tool installed (match diameter in PenguinCAM)
- [ ] Material securely clamped
- [ ] Sacrifice board under material
- [ ] X/Y/Z zeros set correctly
- [ ] Dry run completed successfully
- [ ] Clamps won't interfere with tool path
- [ ] Dust collection running
- [ ] Safety glasses on
- [ ] Know where emergency stop is!

**While Running:**
- Stay nearby and watch the machine
- Be ready to hit emergency stop
- Never reach into cutting area
- Don't leave machine unattended

**After Machining:**
- Break off tabs carefully (pliers help)
- Deburr edges with file or sandpaper
- Check dimensions with calipers
- Celebrate! 🎉

---

## ❓ Common Issues & Fixes

### "Part is wrong size"

**Check:**
- Did you specify correct tool diameter in PenguinCAM?
- Is your actual endmill the size you think it is?
- Did you measure material thickness accurately?

### "Holes didn't cut through"

**Check:**
- Z=0 set at sacrifice board (not material top)?
- Sacrifice board gap-free under material?
- Material thickness entered correctly?

### "Part moved during cutting"

**Fix:**
- More clamps! Material must not move at all
- Reduce feed rate for better stability
- Add more tabs (6-8 for large parts)

### "Tabs are hard to break"

**Normal!** That's good - they held the part.

**Tips:**
- Use pliers to twist tabs off
- Score tab with utility knife first
- File/sand remaining material flush

### "Tool hit my clamps!"

**Prevention:**
- Always dry run first
- Position clamps outside tool path
- Check 3D preview for clearances
- Use low-profile clamps if needed

---

## 📞 Getting Help

**For PenguinCAM Problems:**
- Check this guide first
- Ask a mentor
- Report bugs: [GitHub Issues]

**For CNC Machine Problems:**
- Emergency: Hit E-stop immediately
- Ask mentor or experienced student
- Check machine manual
- Don't guess - ask for help!

**For Design Questions:**
- Review Onshape part - is it 2D?
- Check hole sizes match table above
- Ask mentor for CAD review

---

## 🎓 Best Practices

### For Students:

1. **Always dry run first** - catch mistakes before breaking tools
2. **Measure material thickness** - don't guess!
3. **Check tool diameter** - wrong tool = wrong size part
4. **Save to Drive** - helps whole team
5. **Stay safe** - machines are powerful and deserve respect

### For Mentors:

1. **Review student G-code** before first run
2. **Verify zeroing procedure** - most common mistake
3. **Start conservative** - slow feed rates until proven
4. **Keep spare endmills** - they break, especially while learning
5. **Celebrate successes** - first successful part is a big deal!

---

## 🔗 More Information

**Documentation:**
- [Tool Compensation Guide](TOOL_COMPENSATION_GUIDE.md) - How offsets work
- [Z-Coordinate System](Z_COORDINATE_SYSTEM.md) - Detailed zeroing guide
- [Deployment Guide](DEPLOYMENT_GUIDE.md) - For mentors setting up PenguinCAM
- [Roadmap](../ROADMAP.md) - Upcoming features

**Questions?**
- Ask your mentor
- Check team documentation
- GitHub: [Your repo link]

---

## 📤 Alternative: Manual DXF Upload

**If Onshape extension isn't available:**

1. **In Onshape:**
   - Right-click the face you want to machine
   - Export → DXF
   - Save the file

2. **In PenguinCAM:**
   - Go to https://penguincam.popcornpenguins.com
   - Sign in with @popcornpenguins.com account
   - Drag & drop DXF file (or click to browse)
   
3. **Continue as normal:**
   - Orient part in Setup Mode
   - Generate G-code
   - Download or save to Drive

**Same result, just an extra step!**

---

**Go Popcorn Penguins! 🍿🐧**

*Stay safe, machine smart, and build awesome robots!*
