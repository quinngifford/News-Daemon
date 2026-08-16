#!/usr/bin/env python3
"""Generate TrumpDeathWatcher.xcodeproj from the files on disk.

A .pbxproj is a graph of objects keyed by 24-hex-digit identifiers, and every
file appears in it three times — as a reference, as a build file, and in a
group. Hand-maintaining that is how projects end up with a source file that
silently is not compiled, so it is generated instead: run this after adding,
removing or moving a Swift file and the project matches the disk again.

    python3 ios/tools/generate_project.py

Identifiers are an MD5 of (role, path), so re-running produces a byte-identical
project and the diff stays readable.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # ios/
APP = "TrumpDeathWatcher"
BUNDLE_ID = "com.trumpdeathwatcher.app"
DEPLOYMENT_TARGET = "17.0"
SWIFT_VERSION = "5.0"

SOURCE_DIR = ROOT / APP
PROJECT_DIR = ROOT / f"{APP}.xcodeproj"


def uid(role: str, path: str) -> str:
    """Deterministic 24-hex-digit object id."""
    return hashlib.md5(f"{role}:{path}".encode()).hexdigest()[:24].upper()


def file_type(path: Path) -> str:
    return {
        ".swift": "sourcecode.swift",
        ".plist": "text.plist.xml",
        ".entitlements": "text.plist.entitlements",
        ".xcassets": "folder.assetcatalog",
        ".png": "image.png",
        ".md": "net.daringfireball.markdown",
    }.get(path.suffix, "text")


def collect() -> tuple[list[Path], list[Path]]:
    """Swift sources and bundled resources, relative to ios/."""
    sources = sorted(
        p.relative_to(ROOT) for p in SOURCE_DIR.rglob("*.swift")
    )
    resources = [Path(APP) / "Assets.xcassets"]
    return sources, resources


class Tree:
    """Directory tree of PBXGroups, so the navigator mirrors the filesystem."""

    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.children: dict[str, Tree] = {}
        self.files: list[Path] = []

    def add(self, rel: Path) -> None:
        node = self
        for part in rel.parts[1:-1]:            # skip the app dir, keep dirs
            node = node.children.setdefault(part, Tree(part, part))
        node.files.append(rel)


def build_groups(tree: Tree, extra: list[Path], out: list[str]) -> str:
    """Emit PBXGroup entries depth-first; returns the group's id."""
    group_id = uid("group", tree.path or tree.name)
    children = []
    for name in sorted(tree.children):
        children.append((name, build_groups(tree.children[name], [], out)))
    for f in sorted(tree.files) + sorted(extra):
        children.append((f.name, uid("file", str(f))))

    lines = [f"\t\t{group_id} /* {tree.name} */ = {{",
             "\t\t\tisa = PBXGroup;",
             "\t\t\tchildren = ("]
    for name, cid in children:
        lines.append(f"\t\t\t\t{cid} /* {name} */,")
    lines.append("\t\t\t);")
    if tree.path:
        lines.append(f"\t\t\tpath = {tree.path};")
    else:
        lines.append(f"\t\t\tname = {tree.name};")
    lines.append('\t\t\tsourceTree = "<group>";')
    lines.append("\t\t};")
    out.append("\n".join(lines))
    return group_id


def generate() -> str:
    sources, resources = collect()
    plist = Path(APP) / "Info.plist"
    entitlements = Path(APP) / f"{APP}.entitlements"

    target_id = uid("target", APP)
    project_id = uid("project", APP)
    product_id = uid("product", f"{APP}.app")
    main_group_id = uid("group", "")
    products_group_id = uid("group", "Products")
    sources_phase = uid("phase", "sources")
    frameworks_phase = uid("phase", "frameworks")
    resources_phase = uid("phase", "resources")
    project_config_list = uid("configlist", "project")
    target_config_list = uid("configlist", "target")

    # --- PBXBuildFile ------------------------------------------------------
    build_files = []
    for f in sources + resources:
        build_files.append(
            f"\t\t{uid('build', str(f))} /* {f.name} in "
            f"{'Sources' if f.suffix == '.swift' else 'Resources'} */ = "
            f"{{isa = PBXBuildFile; fileRef = {uid('file', str(f))} /* {f.name} */; }};"
        )

    # --- PBXFileReference --------------------------------------------------
    refs = [
        f"\t\t{product_id} /* {APP}.app */ = {{isa = PBXFileReference; "
        f"explicitFileType = wrapper.application; includeInIndex = 0; "
        f'path = "{APP}.app"; sourceTree = BUILT_PRODUCTS_DIR; }};'
    ]
    for f in sources + resources + [plist, entitlements]:
        refs.append(
            f"\t\t{uid('file', str(f))} /* {f.name} */ = {{isa = PBXFileReference; "
            f"lastKnownFileType = {file_type(f)}; path = {f.name}; "
            f'sourceTree = "<group>"; }};'
        )

    # --- PBXGroup ----------------------------------------------------------
    tree = Tree(APP, APP)
    for f in sources:
        tree.add(f)
    groups: list[str] = []
    app_group_id = build_groups(tree, [Path(APP) / "Assets.xcassets", plist,
                                       entitlements], groups)

    groups.append("\n".join([
        f"\t\t{products_group_id} /* Products */ = {{",
        "\t\t\tisa = PBXGroup;",
        "\t\t\tchildren = (",
        f"\t\t\t\t{product_id} /* {APP}.app */,",
        "\t\t\t);",
        "\t\t\tname = Products;",
        '\t\t\tsourceTree = "<group>";',
        "\t\t};",
    ]))
    groups.append("\n".join([
        f"\t\t{main_group_id} = {{",
        "\t\t\tisa = PBXGroup;",
        "\t\t\tchildren = (",
        f"\t\t\t\t{app_group_id} /* {APP} */,",
        f"\t\t\t\t{products_group_id} /* Products */,",
        "\t\t\t);",
        '\t\t\tsourceTree = "<group>";',
        "\t\t};",
    ]))

    # --- build settings ----------------------------------------------------
    shared = {
        "ALWAYS_SEARCH_USER_PATHS": "NO",
        "CLANG_ANALYZER_NONNULL": "YES",
        "CLANG_ENABLE_MODULES": "YES",
        "CLANG_ENABLE_OBJC_ARC": "YES",
        "COPY_PHASE_STRIP": "NO",
        "ENABLE_STRICT_OBJC_MSGSEND": "YES",
        "ENABLE_USER_SCRIPT_SANDBOXING": "YES",
        "GCC_C_LANGUAGE_STANDARD": "gnu17",
        "GCC_NO_COMMON_BLOCKS": "YES",
        "IPHONEOS_DEPLOYMENT_TARGET": DEPLOYMENT_TARGET,
        "LOCALIZATION_PREFERS_STRING_CATALOGS": "YES",
        "MTL_FAST_MATH": "YES",
        "SDKROOT": "iphoneos",
        "SWIFT_EMIT_LOC_STRINGS": "YES",
    }
    project_debug = {
        **shared,
        "DEBUG_INFORMATION_FORMAT": "dwarf",
        "ENABLE_TESTABILITY": "YES",
        "GCC_DYNAMIC_NO_PIC": "NO",
        "GCC_OPTIMIZATION_LEVEL": "0",
        "GCC_PREPROCESSOR_DEFINITIONS": '(\n\t\t\t\t\t"DEBUG=1",\n\t\t\t\t\t"$(inherited)",\n\t\t\t\t)',
        "MTL_ENABLE_DEBUG_INFO": "INCLUDE_SOURCE",
        "ONLY_ACTIVE_ARCH": "YES",
        "SWIFT_ACTIVE_COMPILATION_CONDITIONS": '"DEBUG $(inherited)"',
        "SWIFT_OPTIMIZATION_LEVEL": '"-Onone"',
    }
    project_release = {
        **shared,
        "DEBUG_INFORMATION_FORMAT": '"dwarf-with-dsym"',
        "ENABLE_NS_ASSERTIONS": "NO",
        "MTL_ENABLE_DEBUG_INFO": "NO",
        "SWIFT_COMPILATION_MODE": "wholemodule",
        "VALIDATE_PRODUCT": "YES",
    }
    target_common = {
        "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon",
        "ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME": "AccentColor",
        "CODE_SIGN_ENTITLEMENTS": f"{APP}/{APP}.entitlements",
        "CODE_SIGN_STYLE": "Automatic",
        "CURRENT_PROJECT_VERSION": "1",
        "ENABLE_PREVIEWS": "YES",
        "GENERATE_INFOPLIST_FILE": "NO",
        "INFOPLIST_FILE": f"{APP}/Info.plist",
        "LD_RUNPATH_SEARCH_PATHS": '(\n\t\t\t\t\t"$(inherited)",\n\t\t\t\t\t"@executable_path/Frameworks",\n\t\t\t\t)',
        "MARKETING_VERSION": "1.0",
        "PRODUCT_BUNDLE_IDENTIFIER": BUNDLE_ID,
        "PRODUCT_NAME": '"$(TARGET_NAME)"',
        "SWIFT_VERSION": SWIFT_VERSION,
        "TARGETED_DEVICE_FAMILY": "1",
    }

    def settings_block(settings: dict[str, str]) -> str:
        return "\n".join(f"\t\t\t\t{k} = {v};" for k, v in sorted(settings.items()))

    configs = []
    for name, config_id, settings in [
        ("Debug", uid("config", "project-debug"), project_debug),
        ("Release", uid("config", "project-release"), project_release),
        ("Debug", uid("config", "target-debug"), target_common),
        ("Release", uid("config", "target-release"), target_common),
    ]:
        configs.append("\n".join([
            f"\t\t{config_id} /* {name} */ = {{",
            "\t\t\tisa = XCBuildConfiguration;",
            "\t\t\tbuildSettings = {",
            settings_block(settings),
            "\t\t\t};",
            f"\t\t\tname = {name};",
            "\t\t};",
        ]))

    config_lists = []
    for list_id, label, debug_id, release_id in [
        (project_config_list, f'PBXProject "{APP}"',
         uid("config", "project-debug"), uid("config", "project-release")),
        (target_config_list, f'PBXNativeTarget "{APP}"',
         uid("config", "target-debug"), uid("config", "target-release")),
    ]:
        config_lists.append("\n".join([
            f"\t\t{list_id} /* Build configuration list for {label} */ = {{",
            "\t\t\tisa = XCConfigurationList;",
            "\t\t\tbuildConfigurations = (",
            f"\t\t\t\t{debug_id} /* Debug */,",
            f"\t\t\t\t{release_id} /* Release */,",
            "\t\t\t);",
            "\t\t\tdefaultConfigurationIsVisible = 0;",
            "\t\t\tdefaultConfigurationName = Release;",
            "\t\t};",
        ]))

    swift_build_refs = "\n".join(
        f"\t\t\t\t{uid('build', str(f))} /* {f.name} in Sources */,"
        for f in sources
    )
    resource_build_refs = "\n".join(
        f"\t\t\t\t{uid('build', str(f))} /* {f.name} in Resources */,"
        for f in resources
    )

    return f"""// !$*UTF8*$!
{{
	archiveVersion = 1;
	classes = {{
	}};
	objectVersion = 56;
	objects = {{

/* Begin PBXBuildFile section */
{chr(10).join(build_files)}
/* End PBXBuildFile section */

/* Begin PBXFileReference section */
{chr(10).join(refs)}
/* End PBXFileReference section */

/* Begin PBXFrameworksBuildPhase section */
		{frameworks_phase} /* Frameworks */ = {{
			isa = PBXFrameworksBuildPhase;
			buildActionMask = 2147483647;
			files = (
			);
			runOnlyForDeploymentPostprocessing = 0;
		}};
/* End PBXFrameworksBuildPhase section */

/* Begin PBXGroup section */
{chr(10).join(groups)}
/* End PBXGroup section */

/* Begin PBXNativeTarget section */
		{target_id} /* {APP} */ = {{
			isa = PBXNativeTarget;
			buildConfigurationList = {target_config_list} /* Build configuration list for PBXNativeTarget "{APP}" */;
			buildPhases = (
				{sources_phase} /* Sources */,
				{frameworks_phase} /* Frameworks */,
				{resources_phase} /* Resources */,
			);
			buildRules = (
			);
			dependencies = (
			);
			name = {APP};
			productName = {APP};
			productReference = {product_id} /* {APP}.app */;
			productType = "com.apple.product-type.application";
		}};
/* End PBXNativeTarget section */

/* Begin PBXProject section */
		{project_id} /* Project object */ = {{
			isa = PBXProject;
			attributes = {{
				BuildIndependentTargetsInParallel = 1;
				LastSwiftUpdateCheck = 1600;
				LastUpgradeCheck = 1600;
				TargetAttributes = {{
					{target_id} = {{
						CreatedOnToolsVersion = 16.0;
						SystemCapabilities = {{
							com.apple.Push = {{
								enabled = 1;
							}};
						}};
					}};
				}};
			}};
			buildConfigurationList = {project_config_list} /* Build configuration list for PBXProject "{APP}" */;
			compatibilityVersion = "Xcode 14.0";
			developmentRegion = en;
			hasScannedForEncodings = 0;
			knownRegions = (
				en,
				Base,
			);
			mainGroup = {main_group_id};
			productRefGroup = {products_group_id} /* Products */;
			projectDirPath = "";
			projectRoot = "";
			targets = (
				{target_id} /* {APP} */,
			);
		}};
/* End PBXProject section */

/* Begin PBXResourcesBuildPhase section */
		{resources_phase} /* Resources */ = {{
			isa = PBXResourcesBuildPhase;
			buildActionMask = 2147483647;
			files = (
{resource_build_refs}
			);
			runOnlyForDeploymentPostprocessing = 0;
		}};
/* End PBXResourcesBuildPhase section */

/* Begin PBXSourcesBuildPhase section */
		{sources_phase} /* Sources */ = {{
			isa = PBXSourcesBuildPhase;
			buildActionMask = 2147483647;
			files = (
{swift_build_refs}
			);
			runOnlyForDeploymentPostprocessing = 0;
		}};
/* End PBXSourcesBuildPhase section */

/* Begin XCBuildConfiguration section */
{chr(10).join(configs)}
/* End XCBuildConfiguration section */

/* Begin XCConfigurationList section */
{chr(10).join(config_lists)}
/* End XCConfigurationList section */
	}};
	rootObject = {project_id} /* Project object */;
}}
"""


SCHEME = """<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion = "1600" version = "1.7">
   <BuildAction parallelizeBuildables = "YES" buildImplicitDependencies = "YES">
      <BuildActionEntries>
         <BuildActionEntry buildForTesting = "YES" buildForRunning = "YES"
                           buildForProfiling = "YES" buildForArchiving = "YES"
                           buildForAnalyzing = "YES">
            <BuildableReference
               BuildableIdentifier = "primary"
               BlueprintIdentifier = "{target_id}"
               BuildableName = "{app}.app"
               BlueprintName = "{app}"
               ReferencedContainer = "container:{app}.xcodeproj">
            </BuildableReference>
         </BuildActionEntry>
      </BuildActionEntries>
   </BuildAction>
   <TestAction buildConfiguration = "Debug"
               selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"
               selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"
               shouldUseLaunchSchemeArgsEnv = "YES">
      <Testables>
      </Testables>
   </TestAction>
   <LaunchAction buildConfiguration = "Debug"
                 selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"
                 selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"
                 launchStyle = "0" useCustomWorkingDirectory = "NO"
                 ignoresPersistentStateOnLaunch = "NO" debugDocumentVersioning = "YES"
                 debugServiceExtension = "internal" allowLocationSimulation = "YES">
      <BuildableProductRunnable runnableDebuggingMode = "0">
         <BuildableReference
            BuildableIdentifier = "primary"
            BlueprintIdentifier = "{target_id}"
            BuildableName = "{app}.app"
            BlueprintName = "{app}"
            ReferencedContainer = "container:{app}.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </LaunchAction>
   <ProfileAction buildConfiguration = "Release" shouldUseLaunchSchemeArgsEnv = "YES"
                  savedToolIdentifier = "" useCustomWorkingDirectory = "NO"
                  debugDocumentVersioning = "YES">
      <BuildableProductRunnable runnableDebuggingMode = "0">
         <BuildableReference
            BuildableIdentifier = "primary"
            BlueprintIdentifier = "{target_id}"
            BuildableName = "{app}.app"
            BlueprintName = "{app}"
            ReferencedContainer = "container:{app}.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </ProfileAction>
   <AnalyzeAction buildConfiguration = "Debug">
   </AnalyzeAction>
   <ArchiveAction buildConfiguration = "Release" revealArchiveInOrganizer = "YES">
   </ArchiveAction>
</Scheme>
"""


def main() -> None:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECT_DIR / "project.pbxproj").write_text(generate())

    schemes = PROJECT_DIR / "xcshareddata" / "xcschemes"
    schemes.mkdir(parents=True, exist_ok=True)
    (schemes / f"{APP}.xcscheme").write_text(
        SCHEME.format(target_id=uid("target", APP), app=APP)
    )

    sources, _ = collect()
    print(f"wrote {PROJECT_DIR.relative_to(ROOT.parent)} "
          f"({len(sources)} Swift files)")
    for f in sources:
        print(f"  {f}")


if __name__ == "__main__":
    main()
