bl_info = {
    "name" : "SD CGraph Renderer",
    "author" : "oobma (Gemini, Final Architect)",
    "description" : "Renders SD_CGraph lines with custom colors",
    "blender" : (4, 0, 0),
    "version" : (0, 1, 7),
    "location" : "3D View > Sidebar > SD CGraph Renderer",
    "category" : "3D View"
}

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector, Color

# State Variables
draw_handler = None
is_drawing_active = False
AUTO_NODE_NAME = "SNA_Auto_CurveToMesh"

# ------------------------------------------------------------------------
#   HELPER FUNCTIONS
# ------------------------------------------------------------------------

def get_heatmap_color(value, alpha=1.0):
    """0.0=Blue, 0.5=Green, 1.0=Red"""
    c = Color()
    hue = (1.0 - value) * 0.66
    c.hsv = (hue, 1.0, 1.0)
    return (c.r, c.g, c.b, alpha)

def get_modifier_scale_value(mod):
    """Retrieves graph scale for normalization."""
    candidate_keys = ["Graph Scale", "Input_3", "GraphScale", "Scale"]
    for key in candidate_keys:
        val = mod.get(key)
        if val is not None and isinstance(val, (int, float)):
            return float(val)
    return 1.0

# ------------------------------------------------------------------------
#   AUTO NODE MANAGEMENT
# ------------------------------------------------------------------------

def find_modifier_and_tree(obj):
    for mod in obj.modifiers:
        if mod.type == 'NODES' and mod.node_group:
            if "cgraph" in mod.node_group.name.lower():
                return mod, mod.node_group
    return None, None

def inject_curve_to_mesh(obj):
    mod, tree = find_modifier_and_tree(obj)
    if not tree or AUTO_NODE_NAME in tree.nodes: return

    output_node = None
    for node in tree.nodes:
        if node.type == 'GROUP_OUTPUT' and node.is_active_output:
            output_node = node
            break
    if not output_node:
        for node in tree.nodes:
            if node.type == 'GROUP_OUTPUT': output_node = node; break
            
    if not output_node: return

    socket_in = output_node.inputs[0]
    if not socket_in.is_linked: return

    link = socket_in.links[0]
    from_socket = link.from_socket
    
    c2m_node = tree.nodes.new("GeometryNodeCurveToMesh")
    c2m_node.name = AUTO_NODE_NAME
    c2m_node.label = "Auto Renderer Fix"
    c2m_node.select = False
    c2m_node.location = (output_node.location.x - 200, output_node.location.y)

    tree.links.remove(link)
    tree.links.new(from_socket, c2m_node.inputs['Curve'])
    tree.links.new(c2m_node.outputs['Mesh'], socket_in)

def remove_curve_to_mesh(obj):
    mod, tree = find_modifier_and_tree(obj)
    if not tree: return
    if AUTO_NODE_NAME not in tree.nodes: return

    node_to_del = tree.nodes[AUTO_NODE_NAME]
    input_socket = node_to_del.inputs['Curve']
    output_socket = node_to_del.outputs['Mesh']
    
    if input_socket.is_linked and output_socket.is_linked:
        from_socket = input_socket.links[0].from_socket
        for link in output_socket.links:
            to_socket = link.to_socket
            tree.links.new(from_socket, to_socket)
            
    tree.nodes.remove(node_to_del)


# ------------------------------------------------------------------------
#   SMART DRAWING LOGIC (Topology Filter)
# ------------------------------------------------------------------------

def draw_lines_callback():
    try:
        scene = bpy.context.scene
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except: return

    # --- GPU SETTINGS ---
    gpu.state.line_width_set(getattr(scene, "sd_cgraph_thickness", 2.0))
    if getattr(scene, "sd_cgraph_always_on_top", True):
        gpu.state.depth_test_set('NONE')
    else:
        gpu.state.depth_test_set('LESS_EQUAL')

    # --- SHADER SETUP ---
    try: shader_grad = gpu.shader.from_builtin('SMOOTH_COLOR')
    except: 
        try: shader_grad = gpu.shader.from_builtin('3D_SMOOTH_COLOR')
        except: shader_grad = gpu.shader.from_builtin('UNIFORM_COLOR')

    shader_flat = gpu.shader.from_builtin('UNIFORM_COLOR')

    # Configs
    use_gradient = getattr(scene, "sd_cgraph_use_gradient", False)
    limit_setting = getattr(scene, "sd_cgraph_gradient_max", 1.0) 
    
    # New Config: Color for the crest/connections (Solid white/custom)
    conn_color = getattr(scene, "sd_cgraph_connector_color", (1.0, 1.0, 1.0, 1.0))

    # --- MAIN LOOP ---
    for obj in scene.objects:
        if not obj.visible_get() or obj.type not in {'MESH', 'CURVE'}:
            continue
            
        target_mod = None
        for mod in obj.modifiers:
            if mod.type == 'NODES' and mod.node_group and "cgraph" in mod.node_group.name.lower():
                target_mod = mod
                break
        if not target_mod: continue

        modifier_graph_scale = get_modifier_scale_value(target_mod)
        if modifier_graph_scale == 0: modifier_graph_scale = 0.001 

        eval_obj = obj.evaluated_get(depsgraph)
        try: mesh = eval_obj.to_mesh()
        except RuntimeError: continue

        if mesh and len(mesh.edges) > 0:
            world_mat = obj.matrix_world
            verts_co_world = [world_mat @ v.co for v in mesh.vertices]
            
            # --- TOPOLOGY ANALYSIS (The Smart Part) ---
            # 1. Count connections per vertex
            # If a vertex has 1 edge, it's an end-point. If 2, it's inside a line.
            # Comb "Hairs" are typically isolated segments (Count=1 and Count=1).
            # Connection lines are chains.
            
            vert_edge_counts = [0] * len(mesh.vertices)
            for edge in mesh.edges:
                vert_edge_counts[edge.vertices[0]] += 1
                vert_edge_counts[edge.vertices[1]] += 1
            
            # 2. Lists for drawing batches
            heatmap_pos = []
            heatmap_col = []
            
            connector_pos = [] # For crest/base lines
            
            obj_base_color = getattr(obj, "sd_cgraph_color", (0, 1, 1, 1))

            for edge in mesh.edges:
                i1, i2 = edge.vertices[0], edge.vertices[1]
                v1 = verts_co_world[i1]
                v2 = verts_co_world[i2]
                
                # HEURISTIC:
                # If both vertices of an edge have only 1 connection, 
                # it's a disjoint "Comb Hair".
                is_isolated_hair = (vert_edge_counts[i1] == 1 and vert_edge_counts[i2] == 1)
                
                if is_isolated_hair and use_gradient:
                    # ---> RENDER AS HEATMAP (Vertical Bar)
                    visual_length = (v1 - v2).length
                    real_curvature_val = visual_length / modifier_graph_scale
                    
                    intensity = min(real_curvature_val / limit_setting, 1.0)
                    color_rgba = get_heatmap_color(intensity)
                    
                    heatmap_pos.extend([v1, v2])
                    heatmap_col.extend([color_rgba, color_rgba])
                    
                elif is_isolated_hair and not use_gradient:
                    # ---> RENDER AS SOLID USER COLOR (Vertical Bar)
                    heatmap_pos.extend([v1, v2])
                    heatmap_col.extend([obj_base_color, obj_base_color])
                    
                else:
                    # ---> RENDER AS CONNECTOR (Crest / Base)
                    # We draw these separately with a clean contrast color
                    # to visualize flow/acceleration.
                    connector_pos.extend([v1, v2])

            # --- BATCH DRAWING ---
            
            # 1. Draw Heatmap/Combs
            if heatmap_pos:
                shader_grad.bind()
                batch = batch_for_shader(shader_grad, 'LINES', {"pos": heatmap_pos, "color": heatmap_col})
                batch.draw(shader_grad)
                
            # 2. Draw Connectors (Crests)
            if connector_pos:
                shader_flat.bind()
                # We draw connectors slightly thicker or different color to analyze flow
                shader_flat.uniform_float("color", conn_color) 
                
                # Make connectors slightly distinct
                prev_width = getattr(scene, "sd_cgraph_thickness", 2.0)
                gpu.state.line_width_set(max(1.0, prev_width - 1.0)) # Slightly thinner for precision
                
                batch = batch_for_shader(shader_flat, 'LINES', {"pos": connector_pos})
                batch.draw(shader_flat)
                
                # Restore width
                gpu.state.line_width_set(prev_width)

        eval_obj.to_mesh_clear()

    # Restore GPU State
    if getattr(scene, "sd_cgraph_always_on_top", True):
        gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.line_width_set(1.0)


# ------------------------------------------------------------------------
#   UI & REGISTRATION
# ------------------------------------------------------------------------

class SNA_OT_ToggleRenderer(bpy.types.Operator):
    bl_idname = "sna.toggle_sd_renderer"
    bl_label = "Toggle Renderer"
    
    def execute(self, context):
        global draw_handler, is_drawing_active
        scene_objs = context.scene.objects
        
        if is_drawing_active:
            if draw_handler:
                try: bpy.types.SpaceView3D.draw_handler_remove(draw_handler, 'WINDOW')
                except: pass
            draw_handler = None
            is_drawing_active = False
            for obj in scene_objs: remove_curve_to_mesh(obj)
            self.report({'INFO'}, "SD CGraph: Disabled")
        else:
            for obj in scene_objs: inject_curve_to_mesh(obj)
            if draw_handler is None:
                draw_handler = bpy.types.SpaceView3D.draw_handler_add(draw_lines_callback, (), 'WINDOW', 'POST_VIEW')
            is_drawing_active = True
            self.report({'INFO'}, "SD CGraph: Enabled")
            
        for w in context.window_manager.windows:
            for a in w.screen.areas:
                if a.type == 'VIEW_3D': a.tag_redraw()
        return {"FINISHED"}

class SNA_OT_RefreshNodes(bpy.types.Operator):
    bl_idname = "sna.refresh_sd_nodes"
    bl_label = "Refresh Nodes"
    def execute(self, context):
        if is_drawing_active:
            for obj in context.scene.objects: inject_curve_to_mesh(obj)
            context.area.tag_redraw()
        return {'FINISHED'}

class SNA_PT_RendererPanel(bpy.types.Panel):
    bl_label = "SD CGraph Renderer"
    bl_idname = "SNA_PT_RendererPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SD CGraph Renderer"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        obj = context.active_object
        
        if is_drawing_active:
            row = layout.row(align=True)
            row.operator(SNA_OT_ToggleRenderer.bl_idname, text="Disable", icon='PAUSE', depress=True)
            row.operator(SNA_OT_RefreshNodes.bl_idname, text="", icon='FILE_REFRESH')
        else:
            layout.operator(SNA_OT_ToggleRenderer.bl_idname, text="Enable", icon='PLAY')

        layout.separator()
        
        col = layout.column(align=True)
        col.prop(scene, "sd_cgraph_use_gradient", text="Heatmap Gradient", icon='COLOR')
        
        if scene.sd_cgraph_use_gradient:
            box = col.box()
            box.prop(scene, "sd_cgraph_gradient_max", text="Red Limit (Val)")
            row = box.row()
            row.label(text="Bars: Magnitude", icon='GRAPH')
        else:
            # Color solo si no es Heatmap
            box = layout.box()
            if obj:
                has_mod = False
                for m in obj.modifiers:
                    if m.type=='NODES' and m.node_group and "cgraph" in m.node_group.name.lower(): has_mod=True
                if has_mod:
                    box.prop(obj, "sd_cgraph_color", text="")
                    box.label(text=obj.name, icon='OBJECT_DATA')
                else: box.label(text="Select Obj", icon='INFO')
            else: box.label(text="No Selection")

        layout.separator()
        col = layout.column()
        # Conector / Crest color config
        col.label(text="Connectivity Lines (Flow):")
        col.prop(scene, "sd_cgraph_connector_color", text="")
        
        layout.separator()
        col = layout.column()
        col.label(text="Global Settings:")
        col.prop(scene, "sd_cgraph_thickness", slider=True)
        col.prop(scene, "sd_cgraph_always_on_top", toggle=True)

classes = (SNA_OT_ToggleRenderer, SNA_OT_RefreshNodes, SNA_PT_RendererPanel)

def register():
    bpy.types.Object.sd_cgraph_color = bpy.props.FloatVectorProperty(
        name="Color", subtype='COLOR', default=(0,1,1,1), size=4, min=0, max=1
    )
    bpy.types.Scene.sd_cgraph_thickness = bpy.props.FloatProperty(name="Thickness", default=2, min=1, max=10)
    bpy.types.Scene.sd_cgraph_always_on_top = bpy.props.BoolProperty(name="X-Ray", default=True)
    
    # Heatmap logic
    bpy.types.Scene.sd_cgraph_use_gradient = bpy.props.BoolProperty(name="Use Gradient", default=False)
    bpy.types.Scene.sd_cgraph_gradient_max = bpy.props.FloatProperty(name="Limit", default=0.1, min=0.0001)
    
    # Connector Lines Color
    bpy.types.Scene.sd_cgraph_connector_color = bpy.props.FloatVectorProperty(
        name="Flow Color", subtype='COLOR', default=(1.0, 1.0, 1.0, 1.0), size=4 # Blanco por defecto
    )

    for c in classes: bpy.utils.register_class(c)

def unregister():
    global draw_handler, is_drawing_active
    if is_drawing_active:
        try:
            for o in bpy.data.objects: remove_curve_to_mesh(o)
        except: pass
    if draw_handler:
        try: bpy.types.SpaceView3D.draw_handler_remove(draw_handler, 'WINDOW')
        except: pass
    draw_handler = None
    is_drawing_active = False
    
    for c in reversed(classes): bpy.utils.unregister_class(c)
    del bpy.types.Object.sd_cgraph_color
    del bpy.types.Scene.sd_cgraph_thickness
    del bpy.types.Scene.sd_cgraph_always_on_top
    del bpy.types.Scene.sd_cgraph_use_gradient
    del bpy.types.Scene.sd_cgraph_gradient_max
    del bpy.types.Scene.sd_cgraph_connector_color

if __name__ == "__main__":
    register()
