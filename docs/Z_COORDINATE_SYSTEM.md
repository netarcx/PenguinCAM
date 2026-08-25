# Z-Axis Coordinate System

**Both references are available.** The sacrifice board is the default and everything
below describes it; the wizard's *Zero Z on* control (Setup panel) switches a job to the
top of the stock instead, and `machining.z_reference.datum` in the team config sets which
one a team gets by default.

Whichever you pick, **it is the same cut**: the two programs are identical except that
every Z is shifted by the stock thickness. Nothing about the toolpath, the feeds or the
depth of cut changes - only the surface you touch off on.

| | Sacrifice board (default) | Top of stock |
|---|---|---|
| Touch off on | the spoilboard, through the stock | the top face of the material |
| Material top | `Z = +thickness` | `Z = 0` |
| Material bottom | `Z = 0` | `Z = -thickness` |
| Through-cut | `Z = -0.02"` | `Z = -(thickness + 0.02")` |
| Thickness must be exact? | no | no, but the top face must be where you say |
| Pick it when | sheet work on a spoilboard, several thicknesses in a session | the stock is in a vise, the board is chewed up, or you want the Fusion convention |

The choice reaches the operator in three places: the `(Z-AXIS REFERENCE:)` header block,
the CLI's `Z-AXIS SETUP` printout, and every M0 pause that asks for a re-zero (tool
changes, the deburr swap). All three name the surface the job actually used.

**Tube jobs ignore the setting.** A tube is zeroed to the tube in its jig, and its Z
frame is built by lifting the plate toolpath by (tube height - wall thickness); a
stock-top setting is reset for tube programs rather than applied, and the generator says
so on the console.

## Setting it per job

- **Wizard:** Setup panel -> *Zero Z on* -> Board / Stock top.
- **CLI:** `--z-zero board` or `--z-zero stock-top` (omit to take the team config).
- **Config:** `machining.z_reference.datum: sacrifice_board | stock_top`.
- **Job JSON (`--ops-file`, `/process-job`, `/process-multitool`):** `"z_datum": "stock_top"`.

An unrecognised value is refused rather than defaulted anywhere it can be: a datum that
silently fell back would put Z zero a full material thickness from where the operator set
it. (The one exception is the team config, which warns and uses the board so that one
team's typo cannot stop the app from starting.)

## Why the sacrifice board is the default

### Sacrifice board reference (default)
- **Z=0** is at the SACRIFICE BOARD surface (bottom)
- Material top is at positive Z (e.g., Z=0.25")
- Cut depth is slightly negative (e.g., Z=-0.02")
- Zero to the sacrifice board, the same way every time

### Top surface reference (`Zero Z on: Stock top`)
- **Z=0** is at the TOP surface of the material
- Material bottom is at negative Z (e.g., Z=-0.25")
- Cut depth is the full thickness plus the overcut (e.g., Z=-0.27")
- Zero to the material top, which has to be re-done for each thickness

The reasons the board is the default:

### 1. Consistent Zeroing
- You ALWAYS zero to the sacrifice board
- Don't need to measure material thickness precisely
- Same zero point every time, regardless of material

### 2. Guaranteed Cut-Through
- Can cut slightly into sacrifice board (default: 0.02")
- Ensures complete part separation
- No partial cuts at the bottom

### 3. Material Thickness Doesn't Affect Setup
- Thicker/thinner material? Doesn't matter
- Just set `--thickness` parameter
- Zero point stays the same

## Z Coordinate Examples

**With 0.25" material and 0.02" overcut:**

| Position | Z Coordinate | Description |
|----------|-------------|-------------|
| Safe height | Z=0.35" | 0.1" above material top |
| Material top | Z=0.25" | Top surface |
| Material bottom | Z=0.00" | Sacrifice board surface (ZERO HERE) |
| Cut depth | Z=-0.02" | 0.02" into sacrifice board |

**With 0.125" material and 0.02" overcut:**

| Position | Z Coordinate | Description |
|----------|-------------|-------------|
| Safe height | Z=0.225" | 0.1" above material top |
| Material top | Z=0.125" | Top surface |
| Material bottom | Z=0.00" | Sacrifice board surface (ZERO HERE) |
| Cut depth | Z=-0.02" | 0.02" into sacrifice board |

## New Parameters

### --sacrifice-depth
How far to cut into the sacrifice board (default: 0.02")

**Usage:**
```bash
# Default (0.02" into sacrifice board)
python frc_cam_postprocessor.py part.dxf output.gcode --thickness 0.25

# More aggressive overcut (0.03")
python frc_cam_postprocessor.py part.dxf output.gcode --thickness 0.25 --sacrifice-depth 0.03

# Minimal overcut (0.01")
python frc_cam_postprocessor.py part.dxf output.gcode --thickness 0.25 --sacrifice-depth 0.01
```

**Recommendations:**
- **Aluminum:** 0.02" (default) - adequate
- **Polycarbonate:** 0.02-0.03" - material can flex
- **Wood:** 0.02-0.03" - fibers can cause partial cuts
- **Thin materials (<1/8"):** 0.015" - less overcut needed

## Setup Procedure

### Step 1: Install Material
1. Place material on sacrifice board
2. Clamp or tape down securely
3. Material thickness doesn't need to be exact

### Step 2: Zero X and Y Axes
1. Position tool at the **lower-left corner** of your material
2. This should align with the origin (0,0) in your CAD file
3. Set X=0 and Y=0 in your controller
4. This is your X/Y reference point for the entire job

### Step 3: Zero Z-Axis
1. **Touch off to SACRIFICE BOARD surface** (or the top of the stock, if the job was
   generated with `Zero Z on: Stock top` - the G-code header says which)
2. Set this as Z=0 in your controller
3. Don't mix them up: the two zeros are exactly one material thickness apart, and the
   program has no way to tell which surface the tool is actually touching

### Step 4: Run Program
1. Load G-code
2. Start cycle
3. Tool will:
   - Rapid to safe height (above material)
   - Plunge to material surface
   - Cut through material
   - Cut slightly into sacrifice board
   - Retract to safe height

## G-Code Header

The generated G-code includes clear setup instructions:

```gcode
(Z-AXIS COORDINATE SYSTEM:)
(  Z=0 is at SACRIFICE BOARD (bottom))
(  Material top is at Z=0.2500")
(  Cut depth: Z=-0.0200" (0.0200" into sacrifice board))
(  Safe height: Z=0.3500")
(  ** Zero your Z-axis to the sacrifice board surface **)
```

## Console Output

After generation, the console reminds you:

```
Z-AXIS SETUP:
  ** Zero your Z-axis to the SACRIFICE BOARD surface **
  Material top will be at Z=0.2500"
  Cut depth: Z=-0.0200" (0.0200" into sacrifice board)
  Safe height: Z=0.3500"
```

## Tab Height Calculation

Tabs are calculated correctly in the new system:

```
Cut depth: Z=-0.02" (into sacrifice board)
Tab height: 0.03" (material left in tab)
Tab Z position: -0.02" + 0.03" = 0.01"
```

So tabs are at Z=0.01", which leaves 0.03" of material connecting the part.

## Migration from Old System

If you have old G-code files (before this update):

**DO NOT USE THEM!** The Z coordinates are incompatible.

Regenerate all G-code with the new post-processor.

## Troubleshooting

### "Tool crashes into sacrifice board"
- Check that you zeroed to sacrifice board, not material top
- Verify `--thickness` parameter matches actual material

### "Part not fully cut through"
- Increase `--sacrifice-depth` to 0.03" or 0.04"
- Check that sacrifice board is flat and level
- Verify Z-axis is actually zeroed to board surface

### "Too much material removed from sacrifice board"
- Reduce `--sacrifice-depth` to 0.01"
- Check Z-axis zero procedure
- Consider replacing worn sacrifice board

### "Can't zero to sacrifice board - it's too damaged"
- Replace or flip sacrifice board
- Use a straight edge to find highest point
- Zero to that point instead

## Benefits for FRC Teams

### 1. Faster Setup
- No measuring material thickness with calipers
- No touching off to material surface
- One zero point, every time

### 2. More Reliable
- Guaranteed cut-through every time
- No partial cuts at bottom
- No "almost through" problems

### 3. Easier for New Students
- Simpler zeroing procedure
- Less chance of error
- Clear instructions in G-code header

### 4. Better for Production
- Batch cutting multiple parts
- Don't re-zero between parts (if same thickness)
- Faster turnaround

## Example Workflow

**Cutting 5 identical aluminum plates:**

1. **One-time setup:**
   - Place sacrifice board on CNC
   - Zero Z-axis to sacrifice board
   - DONE - don't touch Z-axis again!

2. **For each plate:**
   - Place 1/4" aluminum on board
   - Tape down
   - Load G-code (same file every time)
   - Run program
   - Remove part
   - Repeat

3. **No re-zeroing needed** between parts!

## Technical Details

### Z Coordinate Calculations

In the code:
```python
self.material_thickness = 0.25          # User input
self.sacrifice_board_depth = 0.02       # User input (default)
self.clearance_height = 0.1             # Fixed

# Calculated values:
self.safe_height = 0.25 + 0.1 = 0.35    # Above material
self.material_top = 0.25                 # Top surface
self.cut_depth = -0.02                   # Into sacrifice board
```

### All Z Moves Updated

Every Z coordinate in the G-code has been updated:
- ✅ Safe height moves (G0 Z...)
- ✅ Plunge moves (G1 Z... for drilling)
- ✅ Helical moves (G2 ... Z... for holes)
- ✅ Tab heights (G1 Z... for tabs)
- ✅ Pocket plunges
- ✅ Perimeter cuts

## Compatibility

This change affects:
- ✅ Main post-processor script
- ⚠️ Safe test mode (needs update)
- ⚠️ Batch processing scripts (use new syntax)

## Summary

✅ **New workflow is better in every way:**
- Easier setup (always zero to sacrifice board)
- More reliable (guaranteed cut-through with overcut)
- Faster (no re-zeroing between parts)
- Clearer (G-code header explains setup)

✅ **Default settings work great:**
- 0.02" overcut is good for most materials
- Adjust if needed with `--sacrifice-depth`

✅ **All Z coordinates automatically calculated:**
- Safe height = material thickness + 0.1"
- Material top = material thickness
- Cut depth = -sacrifice board depth

**Just zero to the sacrifice board and go!** 🎯
